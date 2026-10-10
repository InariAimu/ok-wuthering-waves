"""Step 2: preview and run mathematical-model Echo enhancement.

Simulation mode is deliberately the default. It exercises the same model
input, status publishing, decision log and report hand-off without touching
the game UI or consuming resources.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Mapping

import cv2
import numpy as np

from src.echo.adaptive import AdaptiveEnhancementSession, AdaptivePolicy, simulate_adaptive_session
from src.echo.api import EchoResourceSnapshot, ResourceSource
from src.echo.decision_log import DecisionJournal
from src.echo.optimizer import CampaignEvent, CampaignResult
from src.echo.simulation import EchoStat
from src.echo.workflow import (
    EchoInventoryCheckResult,
    named_echo_build_to_dict,
    policy_config_to_dict,
)
from src.task.EnhanceEchoTask import (
    EnhanceEchoTask, NoEligibleFreshEchoError, ECHO_INVENTORY_COST_TABS,
    ECHO_INVENTORY_GRID_COLUMNS, ECHO_INVENTORY_GRID_ROWS,
    ECHO_INVENTORY_LEVEL_ROWS, ECHO_INVENTORY_SORT_DIRECTION,
)


STAT_LABELS = {
    EchoStat.CRIT_RATE: "暴击",
    EchoStat.CRIT_DAMAGE: "暴击伤害",
    EchoStat.ATTACK_PERCENT: "攻击%",
    EchoStat.DEFENSE_PERCENT: "防御%",
    EchoStat.HP_PERCENT: "生命%",
    EchoStat.ATTACK_FLAT: "攻击",
    EchoStat.DEFENSE_FLAT: "防御",
    EchoStat.HP_FLAT: "生命",
    EchoStat.ENERGY_REGEN: "共鸣效率",
    EchoStat.BASIC_ATTACK_DAMAGE: "普攻伤害",
    EchoStat.HEAVY_ATTACK_DAMAGE: "重击伤害",
    EchoStat.RESONANCE_SKILL_DAMAGE: "共鸣技能伤害",
    EchoStat.RESONANCE_LIBERATION_DAMAGE: "共鸣解放伤害",
}

MAIN_STAT_LABELS = {
    "attack_percent": "攻击%",
    "defense_percent": "防御%",
    "hp_percent": "生命%",
    "crit_rate": "暴击",
    "crit_damage": "暴击伤害",
    "energy_regen": "共鸣效率",
    "aero_damage": "气动伤害",
    "electro_damage": "导电伤害",
    "fusion_damage": "热熔伤害",
    "glacio_damage": "冷凝伤害",
    "havoc_damage": "湮灭伤害",
    "spectro_damage": "衍射伤害",
    "healing_bonus": "治疗效果",
}

FLAT_STATS = {
    EchoStat.ATTACK_FLAT,
    EchoStat.DEFENSE_FLAT,
    EchoStat.HP_FLAT,
}

ACTION_LABELS = {
    "start": "选择新胚子",
    "continue": "模拟开孔",
    "replace_success": "模拟替换成功",
    "keep_reserve": "模拟保留备选",
    "discard_failure": "模拟弃置",
    "discard_resource_stop": "资源不足停止",
}

REASON_LABELS = {
    "maximum attempts reached": "达到模拟最大尝试次数",
    "resources cannot pay for the first +5 reveal": "模拟资源不足以进行第一次+5开孔",
    "resources cannot pay for the next staged reveal": "模拟资源不足以进行下一阶段开孔",
    "selected the fresh candidate with the highest expected gain per limiting-resource fraction":
        "选择单位瓶颈资源期望提升最高的新胚子",
    "candidate is the best valid replacement among all compatible same-COST slots":
        "候选是全部同COST兼容位置中的最佳有效替换",
    "full candidate has no energy-feasible replacement above the minimum gain":
        "满级候选没有满足共鸣效率和最低提升要求的替换位置",
    "conditioned completion efficiency fell below the fresh-candidate opportunity cost":
        "当前半成品的完成效率低于新胚子机会成本",
    "no sampled valid completion remains": "抽样中已不存在有效完成结果",
    "remaining slots cannot satisfy all required substats": "剩余孔数不足以满足全部必需词条",
    "conditioned completion efficiency meets the rolling fresh-candidate benchmark":
        "当前半成品的完成效率达到滚动新胚子基准",
}


class ModelEnhanceEchoTask(EnhanceEchoTask):
    """Enhancement task that consumes a reviewed step-one result."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = "② 使用检查结果进行数学模型强化"
        self.description = (
            "先显示当前五个位置、模型总属性及最大提升位置。"
            "默认只进行离线模拟并记录全部决策，不操作游戏；关闭模拟后才进入实机强化。"
        )
        retained_keys = (
            "Pause after Success",
            "Resource Source",
            "Manual Tuners",
            "Manual Premium Echo EXP Tubes",
            "Manual Echo EXP",
            "Max Candidates Per Run",
            "Max Candidate Search Pages",
            "Candidate Search Scroll Count",
        )
        self.default_config = {
            key: self.default_config[key] for key in retained_keys
        }
        self.default_config.update({
            "Inspection Result File": "",
            "Simulation Only": True,
            "Simulation Attempts": 20,
            "Simulation Rollout Samples": 300,
            "Simulation Tuners": 1000,
            "Simulation Premium Echo EXP Tubes": 300,
            "Simulation Output Directory": "artifacts/echo_inventory/simulations",
            "Max Candidates Per Run": 0,
            "Max Candidate Search Pages": 30,
            "Pause after Success": False,
            "Candidate Switch Ratio": 1.25,
            "Missing Candidate Stop Ratio": 0.5,
            "Fresh Candidate Samples": 20000,
            "Maximum Decision Samples": 80000,
            "Decision Log Directory": "artifacts/echo_inventory/enhancement_runs",
        })
        self.config_type = {
            "Resource Source": {
                "type": "drop_down",
                "options": ["OK-WW or Manual", "Manual"],
            },
        }
        self.config_description = {
            key: self.config_description[key] for key in retained_keys
        }
        self.config_description.update({
            "Inspection Result File": (
                "第一步生成并经用户检视的 echo_inventory_check.json 路径"
            ),
            "Simulation Only": (
                "开发安全模式；只运行数学模拟、更新任务信息和写日志，绝不操作游戏"
            ),
            "Simulation Attempts": "一次模拟最多尝试的声骸胚子数",
            "Simulation Rollout Samples": "每个决策状态的蒙特卡洛样本数",
            "Simulation Tuners": "模拟可用调谐器，不读取或消耗游戏库存",
            "Simulation Premium Echo EXP Tubes": (
                "模拟可用特级密音筒等价数，每个按5000经验计算"
            ),
            "Simulation Output Directory": "模拟 JSON 决策日志的输出目录",
            "Candidate Switch Ratio": "Switch when another available candidate is this many times as efficient",
            "Missing Candidate Stop Ratio": "Stop when available efficiency is below this fraction of the best missing candidate",
            "Fresh Candidate Samples": "Samples used to compare fresh candidates with joint inventory optimization",
            "Maximum Decision Samples": "Maximum samples used when a decision is near a switching or stopping threshold",
            "Decision Log Directory": "Directory for JSONL decisions, current JSON state and a readable Markdown summary",
        })
        self._loaded_inspection_result: EchoInventoryCheckResult | None = None

    @staticmethod
    def _format_value(stat: EchoStat, value: float) -> str:
        if stat in FLAT_STATS:
            return str(int(value)) if float(value).is_integer() else f"{value:.1f}"
        return f"{value:.1f}%"

    @classmethod
    def _format_rolls(cls, rolls) -> str:
        return "，".join(
            f"{STAT_LABELS[roll.stat]} {cls._format_value(roll.stat, roll.value)}"
            for roll in rolls
        ) or "无副词条"

    @staticmethod
    def _main_stat_label(compatibility: str) -> str:
        raw = compatibility.rsplit(":", 1)[-1]
        return MAIN_STAT_LABELS.get(raw, raw)

    @classmethod
    def _format_echo(cls, echo) -> str:
        return (
            f"{echo.identity.name} | COST {echo.identity.cost} | "
            f"主属性 {cls._main_stat_label(echo.identity.compatibility)} | "
            f"{cls._format_rolls(echo.state.rolls)}"
        )

    @classmethod
    def _format_panel(cls, result: EchoInventoryCheckResult) -> str:
        return "，".join(
            f"{STAT_LABELS[stat]} {cls._format_value(stat, value)}"
            for stat, value in result.build.panel.values.items()
            if abs(value) > 1e-12
        ) or "无"

    @staticmethod
    def _largest_inspected_improvement(
            result: EchoInventoryCheckResult) -> dict[str, object] | None:
        raw = result.inspection.get("replacements", [])
        if not isinstance(raw, list):
            return None
        candidates = []
        for entry in raw:
            if not isinstance(entry, Mapping):
                continue
            replacement = entry.get("replacement")
            item = entry.get("item")
            if not isinstance(replacement, Mapping) or not isinstance(item, Mapping):
                continue
            try:
                delta = float(replacement["delta"])
            except (KeyError, TypeError, ValueError):
                continue
            candidates.append({
                "slot": str(replacement.get("slot", "")),
                "delta": delta,
                "final_score": float(replacement.get("final_score", 0)),
                "energy_feasible": bool(replacement.get("energy_feasible", False)),
                "name": str(item.get("name", "")),
                "cost": int(item.get("cost", 0)),
                "compatibility": str(item.get("compatibility", "")),
            })
        return max(candidates, key=lambda item: item["delta"], default=None)

    def _simulation_only(self) -> bool:
        return bool(self.config.get("Simulation Only", True))

    @staticmethod
    def _reason_label(reason: str) -> str:
        if reason.startswith("no sampled fresh candidate can produce"):
            return "抽样中已没有新胚子能够产生有效严格提升"
        return REASON_LABELS.get(reason, reason)

    def _publish_preflight(self, result: EchoInventoryCheckResult) -> None:
        if len(result.build.echoes) != 5:
            raise RuntimeError(
                f"第一步检查结果必须包含当前五个位置，实际为 {len(result.build.echoes)} 个"
            )
        mode = "模拟（不会操作游戏）" if self._simulation_only() else "实机强化"
        self.info_set("运行模式", mode)
        self.info_set("检查结果", result.source_path)
        self.info_set("目标合鸣套装", result.sonata_set)
        self.info_set("当前模型总分", round(result.build.score, 4))
        self.info_set("当前模型总属性", self._format_panel(result))
        for echo in result.build.echoes:
            self.info_set(f"当前位置 {echo.slot}", self._format_echo(echo))

        best = self._largest_inspected_improvement(result)
        if best is None:
            self.info_set("目前最大提升位置", "无")
            self.info_set("目前最大提升声骸", "无")
            self.info_set("目前最大提升分数", 0)
            self.info_set("目标COST分页", "无")
            best_text = "无"
        else:
            self.info_set("目前最大提升位置", best["slot"])
            self.info_set(
                "目前最大提升声骸",
                f"{best['name']} | COST {best['cost']} | "
                f"主属性 {self._main_stat_label(str(best['compatibility']))}",
            )
            self.info_set("目前最大提升分数", round(float(best["delta"]), 4))
            self.info_set(
                "目标COST分页",
                f"COST {best['cost']}（来自最大提升位置 {best['slot']}）",
            )
            best_text = f"{best['slot']} {float(best['delta']):.4f}"

        self.log_info(
            f"第二步预检: 套装={result.sonata_set}, "
            f"位置数={len(result.build.echoes)}, 模型总分={result.build.score:.4f}, "
            f"最大提升={best_text}"
        )

    def load_inspection_result(
            self, path: str | Path) -> EchoInventoryCheckResult:
        result = EchoInventoryCheckResult.read(path)
        self.configure_echo_enhancement_model(
            result.build,
            result.candidate_pool,
            config=result.policy,
        )
        # The inspection's largest single replacement is display information;
        # it must not lock the whole run to one COST tab.
        self._selected_sonata_set = result.sonata_set
        self._loaded_inspection_result = result
        self._publish_preflight(result)
        self.info_set("检查时可提升声骸", result.replacement_count)
        return result

    def _adaptive_policy(self):
        return AdaptivePolicy(
            switch_efficiency_ratio=float(self.config.get('Candidate Switch Ratio', 1.25)),
            missing_candidate_stop_ratio=float(self.config.get('Missing Candidate Stop Ratio', .5)),
            fresh_samples=int(self.config.get('Fresh Candidate Samples', 20000)),
            maximum_samples=int(self.config.get('Maximum Decision Samples', 80000)),
        )

    def _adaptive_event(self, kind, payload):
        if kind == 'candidate_ranking':
            self.info_set('动态目标决策', payload['reason'])
            selected = payload['selected']
            best = payload['best_legal']
            self.info_set('下一强化目标', selected['name'] if selected else '停止开启新胚子')
            self.info_set('最佳合法目标', best['name'] if best else '无')
            missing = [row for row in payload['rankings'] if row['remaining'] == 0
                       and row['relative_efficiency'] >= self._adaptive_policy().missing_candidate_stop_ratio]
            self.info_set('建议获取胚子', '；'.join(
                f"{row['identity']['name']}/COST {row['identity']['cost']}/"
                f"{self._main_stat_label(row['identity']['compatibility'])}" for row in missing) or '无')
            for row in payload['rankings']:
                metrics, identity = row['metrics'], row['identity']
                self.log_info(f"[候选排名] {identity['name']}/COST {identity['cost']} "
                    f"库存={row['remaining']}({row['availability']}) "
                    f"联合提升概率={metrics['success_probability']:.3%} "
                    f"平均正提升={metrics['expected_gain']:.6f} "
                    f"净材料效率={metrics['efficiency']:.4f} "
                    f"相对最佳={row['relative_efficiency']:.3f} 样本={metrics['samples']}")
            self.log_info(f"[动态调度] {payload['action']}: {payload['reason']}",
                          notify=selected is None)
        elif kind == 'joint_optimization':
            self.info_set('联合优化提升', round(payload['score_gain'], 4))
            self.log_info(f"[联合优化] +25后重新组合全部已知库存，提升={payload['score_gain']:.4f}；逻辑配装尚未在游戏中应用")
        elif kind == 'stopped':
            self.info_set('强化停止原因', payload['reason'])
            self.log_info(f"[强化停止] {payload['reason']}")

    def _new_adaptive_session(self, result, resources, *, policy=None, mode='live'):
        adaptive = self._adaptive_policy()
        completed = result.completed_inventory()
        root = Path(str(self.config.get('Decision Log Directory') or
                        'artifacts/echo_inventory/enhancement_runs')).expanduser().resolve()
        directory = root / datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        journal = DecisionJournal(directory)
        self.info_set('强化决策日志', str(directory / 'summary.md'))
        self.log_info(f'强化决策日志: {directory}')
        return AdaptiveEnhancementSession(result.build, result.candidate_pool,
            resources=resources, config=policy or result.policy, completed_inventory=completed,
            # A prior check's stock can be stale. Live mode verifies it through
            # target-specific searches before recording any shortage.
            availability=result.simulated_stock() if mode == 'simulation_only' else None,
            adaptive_policy=adaptive, journal=journal, event_listener=self._adaptive_event,
            mode=mode, source=result.source_path)

    def _prepare_enhancement_runtime(self):
        if getattr(self, '_session_is_external', False) or self._loaded_inspection_result is None:
            return super()._prepare_enhancement_runtime()
        self._manual_resource_ledger = None
        self._echo_enhancement_session = self._new_adaptive_session(
            self._loaded_inspection_result, self._resolve_resources())
        for identity, evidence in getattr(self, '_verified_missing_stock', ()):
            self._echo_enhancement_session.observe_stock(identity, 0, evidence=evidence)
        self._last_resource_sync_completed = 0
        self._publish_resources(self._echo_enhancement_session.ledger)

    def configure_live_observations(self, *, missing_stock=(), search_settle_time=1.5):
        """Use explicitly supplied current-run evidence, never old GUI stock.

        Call before run(), after loading the inspection. Positive quantities
        must still be read from the game; only verified missing identities
        may bypass a redundant search.
        """
        import math
        if not math.isfinite(search_settle_time) or search_settle_time < 0:
            raise ValueError('search_settle_time must be finite and non-negative')
        result = self._loaded_inspection_result
        if result is None:
            raise ValueError('load the current inspection before configuring live observations')
        records = tuple(missing_stock)
        if any(identity not in result.candidate_pool or not str(evidence).strip()
               for identity, evidence in records):
            raise ValueError('missing-stock evidence must identify a current candidate')
        self._verified_missing_stock = records
        self._live_search_settle_time = search_settle_time

    def _read_adaptive_stack_count(self, column, row):
        """A bare card number is quantity only after +0 detail validation."""
        readings = []
        for _ in range(2):
            self.next_frame()
            frame = self.frame
            offset = self._echo_inventory_capture_y_offset(frame)
            x, y = ECHO_INVENTORY_GRID_COLUMNS[column], ECHO_INVENTORY_LEVEL_ROWS[row]
            boxes = self.ocr(x-.002, y-.012+offset, x+.036, y+.028+offset,
                             frame=frame, threshold=.1)
            values = [int(box.name.strip()) for box in boxes
                      if re.fullmatch(r'[1-9]\d{0,4}', box.name.strip())]
            readings.append(values[0] if len(values) == 1 else None)
        return readings[0] if readings[0] == readings[1] else None

    def _adaptive_grid_frame(self):
        self.next_frame()
        frame = self.frame
        offset = self._echo_inventory_capture_y_offset(frame)
        height, width = frame.shape[:2]
        # Inter-card gaps reveal the animated 3D scene. Comparing that scene
        # made an inventory already at its end appear to scroll indefinitely.
        # Keep card interiors, including their level/stack labels;
        # omit gaps and the pulsing selection border. Ambiguous motion inside
        # a card still remains inconclusive under the existing thresholds.
        cards = [
            frame[int((y-.105+offset)*height):int((y+.010+offset)*height),
                  int((x-.025)*width):int((x+.025)*width)].reshape(-1, 3)
            for y in ECHO_INVENTORY_LEVEL_ROWS
            for x in ECHO_INVENTORY_GRID_COLUMNS
        ]
        return np.concatenate(cards, axis=0).copy()

    def _adaptive_scroll_changed(self, count, settle_time):
        before = self._adaptive_grid_frame()
        self.scroll_relative(.25, .70, -count)
        self.sleep(settle_time)
        after = self._adaptive_grid_frame()
        if before.shape != after.shape:
            return True
        difference = cv2.absdiff(before, after)
        # Compare before/after the scroll without intervening card selection.
        # A moving/animated frame remains inconclusive and reaches the limit
        # rather than being silently classified as missing inventory.
        return float(np.mean(difference)) >= .5 or float(np.percentile(difference, 99)) >= 8

    def _find_adaptive_fresh_echo(self, target, settle_time=.5):
        settle_time = max(settle_time, getattr(self, '_live_search_settle_time', .5))
        session = self._echo_enhancement_session
        max_pages = max(1, int(self.config.get('Max Candidate Search Pages') or 10))
        scroll_count = max(1, int(self.config.get('Candidate Search Scroll Count') or 10))
        self.click_relative(*ECHO_INVENTORY_COST_TABS[target.cost], after_sleep=settle_time)
        self._active_candidate_cost = target.cost
        self.select_echo_inventory_level_descending(settle_time=settle_time)
        issues = []
        completed_directions = 0
        search_id = len(session.events)
        for direction in range(2):
            # The level label does not encode direction. Search both orders;
            # toggling twice also resets the viewport left at the last Echo.
            for _ in range(2 if direction == 0 else 1):
                self.click_relative(*ECHO_INVENTORY_SORT_DIRECTION, after_sleep=settle_time)
            for page in range(max_pages):
                self.info_set('候选搜索目标', f'{target.name}/COST {target.cost}')
                self.info_set('候选搜索页', page+1)
                for row, y in enumerate(ECHO_INVENTORY_GRID_ROWS):
                    for column, x in enumerate(ECHO_INVENTORY_GRID_COLUMNS):
                        card_level = self._read_echo_card_level(column, row)
                        if card_level is not None and card_level != 0:
                            continue
                        self.click_relative(x, y, after_sleep=settle_time)
                        level = self._read_selected_echo_level()
                        if level is None:
                            issues.append(f'direction={direction} page={page} cell={row},{column}: unreadable level')
                            continue
                        if level != 0:
                            continue
                        try:
                            identity = self._read_current_echo_candidate_identity()
                            if identity != self._read_current_echo_candidate_identity():
                                raise ValueError('identity changed between reads')
                        except (RuntimeError, ValueError) as error:
                            issues.append(f'direction={direction} page={page} cell={row},{column}: {error}')
                            continue
                        quantity = self._read_adaptive_stack_count(column, row)
                        session.observe_stock(identity, quantity,
                            evidence=f'live +0 details, COST {target.cost}, direction {direction}, page {page}, cell {row},{column}',
                            confirmed_available=True)
                        if identity == target:
                            self._selected_model_candidate_identity = identity
                            return True
                if session.journal:
                    self.next_frame()
                    self._write_echo_inventory_png(session.journal.directory /
                        f'search_{search_id}_{target.cost}_{direction}_{page}.png', self.frame)
                if not self._adaptive_scroll_changed(scroll_count, settle_time):
                    completed_directions += 1
                    break
        if completed_directions != 2 or issues:
            session.emit('search_incomplete', dict(identity=asdict(target),
                completed_directions=completed_directions, issues=issues, page_limit=max_pages))
            raise RuntimeError('候选搜索未完整确认，未将该胚子记为缺货；请检查决策日志和搜索截图')
        session.observe_stock(target, 0, evidence='both level sort directions reached unchanged scroll end; no unreadable details')
        return False

    def _select_first_fresh_echo(self, settle_time=.5):
        session = self._echo_enhancement_session
        if not isinstance(session, AdaptiveEnhancementSession):
            return super()._select_first_fresh_echo(settle_time)
        if (getattr(self, '_echo_resource_provider', None) is not None and
                self.config.get('Resource Source') != 'Manual' and
                session.completed_candidates != getattr(self, '_last_resource_sync_completed', -1)):
            observed = self._resolve_resources()
            if observed.source == ResourceSource.OK_WW:
                session.synchronize_resources(observed)
                self._publish_resources(session.ledger)
            else:
                session.emit('resource_read_unavailable', dict(detail=observed.detail,
                    action='retain current ledger; never reset to the original manual budget'))
            self._last_resource_sync_completed = session.completed_candidates
        while True:
            selection = session.choose_candidate()
            if selection.selected is None:
                session.stop(selection.reason)
                raise NoEligibleFreshEchoError(selection.reason)
            if self._find_adaptive_fresh_echo(selection.selected, settle_time):
                # Search can discover previously unknown compatible names.
                # Re-rank those observations before spending on this card.
                confirmed = session.choose_candidate()
                if confirmed.selected != selection.selected:
                    continue
                session.begin_candidate(selection.selected)
                return 0

    def _stop_for_resources(self, cost):
        if isinstance(self._echo_enhancement_session, AdaptiveEnhancementSession):
            self._echo_enhancement_session.stop('resources cannot pay for the next staged reveal')
        return super()._stop_for_resources(cost)

    def _model_ui_event(self, kind, **details):
        session = self._echo_enhancement_session
        if not isinstance(session, AdaptiveEnhancementSession):
            return
        if kind == 'upgrade_click_intent':
            session.unreconciled_upgrade = True
        elif kind == 'reveal_recorded':
            session.unreconciled_upgrade = False
        if session.journal and kind != 'upgrade_clicked':
            self.next_frame()
            path = session.journal.directory / (
                f'candidate_{session.candidate_number:04d}_plus{details["target_slots"]*5}_{kind}.png')
            self._write_echo_inventory_png(path, self.frame)
            details['screenshot'] = str(path)
        session.emit(kind, details)

    def _publish_simulation_event(self, event: CampaignEvent) -> None:
        action = ACTION_LABELS.get(event.action, event.action)
        self.info_set("模拟事件", event.index)
        self.info_set("模拟尝试", event.attempt)
        self.info_set("模拟当前动作", action)
        self.info_set("模拟候选", event.candidate or "无")
        self.info_set("模拟等级", f"+{event.level or 0}")
        self.info_set("模拟剩余调谐器", round(event.resources_after["tuners"], 1))
        self.info_set("模拟剩余声骸经验", round(event.resources_after["echo_exp"], 1))
        self.log_info(
            f"[模拟强化 #{event.index}] 尝试={event.attempt} 动作={action} "
            f"候选={event.candidate or '无'} 等级=+{event.level or 0} "
            f"调谐器={event.resources_after['tuners']:.1f} "
            f"经验={event.resources_after['echo_exp']:.0f} "
            f"原因={self._reason_label(event.reason)}"
        )

    def _simulation_report_data(
            self, source: EchoInventoryCheckResult,
            campaign: CampaignResult, session=None) -> dict[str, object]:
        ledger = campaign.ledger
        return {
            "kind": "ok-ww.echo-enhancement-simulation",
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "mode": "simulation_only",
            "adaptive_decision_log": str(session.journal.directory) if session and session.journal else None,
            "adaptive_state": session.audit_snapshot() if session else None,
            "safety": {
                "game_ui_touched": False,
                "game_resources_consumed": False,
            },
            "source_check": source.source_path,
            "sonata_set": source.sonata_set,
            "preflight": {
                "current_build": named_echo_build_to_dict(source.build),
                "current_model_score": source.build.score,
                "current_model_panel": {
                    stat.value: value for stat, value in source.build.panel.values.items()
                },
                "largest_inspected_improvement": (
                    self._largest_inspected_improvement(source)
                ),
            },
            "policy": policy_config_to_dict(campaign.config),
            "summary": {
                "attempts": campaign.attempts,
                "successes": campaign.successes,
                "failures": campaign.failures,
                "stop_reason": campaign.stop_reason,
                "initial_score": campaign.initial_build.score,
                "final_score": campaign.final_build.score,
                "score_gain": campaign.final_build.score - campaign.initial_build.score,
                "successes_include_kept_reserves": True,
            },
            "resources": {
                "initial_tuners": ledger.initial_tuners,
                "initial_echo_exp": ledger.initial_exp,
                "remaining_tuners": ledger.tuners,
                "remaining_echo_exp": ledger.echo_exp,
                "gross_tuners": ledger.gross_tuners,
                "gross_echo_exp": ledger.gross_exp,
                "refunded_tuners": ledger.refunded_tuners,
                "refunded_echo_exp": ledger.refunded_exp,
                "net_tuners": ledger.net_tuners,
                "net_echo_exp": ledger.net_exp,
            },
            "final_build": named_echo_build_to_dict(campaign.final_build),
            "events": [asdict(event) for event in campaign.events],
        }

    def _write_simulation_report(
            self, source: EchoInventoryCheckResult,
            campaign: CampaignResult, session=None) -> Path:
        root = Path(
            str(self.config.get("Simulation Output Directory") or
                "artifacts/echo_inventory/simulations")
        ).expanduser().resolve()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        target = root / stamp / "echo_enhancement_simulation.json"
        target.parent.mkdir(parents=True, exist_ok=False)
        target.write_text(
            json.dumps(
                self._simulation_report_data(source, campaign, session),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return target

    def _run_simulation(
            self, result: EchoInventoryCheckResult) -> CampaignResult:
        attempts = int(self.config.get("Simulation Attempts") or 20)
        samples = int(self.config.get("Simulation Rollout Samples") or 300)
        tuners = float(self.config.get("Simulation Tuners") or 0)
        tubes = float(self.config.get("Simulation Premium Echo EXP Tubes") or 0)
        if attempts <= 0 or samples <= 0 or tuners < 0 or tubes < 0:
            raise ValueError("模拟次数、抽样数必须为正，模拟资源不能为负")

        policy = replace(
            result.policy,
            max_attempts=attempts,
            rollout_samples=samples,
        )
        self.info_set("模拟状态", "计算中")
        self.info_set("模拟最大尝试", attempts)
        self.info_set("模拟每状态抽样", samples)
        self.log_info(
            f"开始模拟强化: 尝试上限={attempts}, 每状态抽样={samples}, "
            f"模拟调谐器={tuners:.1f}, 模拟经验={tubes * 5000:.0f}; 不操作游戏"
        )
        session = self._new_adaptive_session(result,
            EchoResourceSnapshot.manual(tuners=tuners, echo_exp=tubes * 5000),
            policy=policy, mode='simulation_only')
        try:
            campaign = simulate_adaptive_session(session, attempts)
        except Exception as error:
            session.stop('模拟异常停止，保留决策状态供检查', error=repr(error))
            raise
        for event in campaign.events:
            self._publish_simulation_event(event)

        report = self._write_simulation_report(result, campaign, session)
        self.info_set("模拟状态", "完成")
        self.info_set("模拟成功", campaign.successes)
        self.info_set("模拟失败", campaign.failures)
        self.info_set("模拟最终总分", round(campaign.final_build.score, 4))
        self.info_set(
            "模拟总提升",
            round(campaign.final_build.score - campaign.initial_build.score, 4),
        )
        self.info_set("模拟停止原因", self._reason_label(campaign.stop_reason))
        self.info_set("模拟报告", str(report))
        self.log_info(
            f"模拟强化完成: 尝试={campaign.attempts}, 成功={campaign.successes}, "
            f"失败={campaign.failures}, "
            f"总提升={campaign.final_build.score - campaign.initial_build.score:.4f}, "
            f"报告={report}"
        )
        return campaign

    def run(self):
        result = self._loaded_inspection_result
        if self._echo_enhancement_session is None and self._echo_enhancement_context is None:
            path = str(self.config.get("Inspection Result File") or "").strip()
            if not path:
                raise RuntimeError("请先执行第一步检查，并选择其 echo_inventory_check.json")
            result = self.load_inspection_result(path)
        elif result is not None:
            self._publish_preflight(result)

        if result is None:
            raise RuntimeError("第二步必须从第一步检查结果启动")
        if self._simulation_only():
            return self._run_simulation(result)

        self.select_echo_sonata_set(result.sonata_set, settle_time=1.5)
        try:
            return super().run()
        except Exception as error:
            session = self._echo_enhancement_session
            if isinstance(session, AdaptiveEnhancementSession):
                session.stop('任务异常停止，保留当前候选与账本供检查', error=repr(error))
            raise
        finally:
            session = self._echo_enhancement_session
            if isinstance(session, AdaptiveEnhancementSession) and not session.stop_reason:
                session.stop('达到本轮处理上限或任务正常返回')
