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
from typing import Mapping

from src.echo.optimizer import CampaignEvent, CampaignResult, StagedUpgradeCampaign
from src.echo.simulation import EchoStat
from src.echo.workflow import (
    EchoInventoryCheckResult,
    named_echo_build_to_dict,
    policy_config_to_dict,
)
from src.task.EnhanceEchoTask import EnhanceEchoTask


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
            "Max Candidates Per Run": 1,
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
        best = self._largest_inspected_improvement(result)
        if best is not None:
            self.set_candidate_cost_order((int(best["cost"]),))
        self._selected_sonata_set = result.sonata_set
        self._loaded_inspection_result = result
        self._publish_preflight(result)
        self.info_set("检查时可提升声骸", result.replacement_count)
        return result

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
            campaign: CampaignResult) -> dict[str, object]:
        ledger = campaign.ledger
        return {
            "kind": "ok-ww.echo-enhancement-simulation",
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "mode": "simulation_only",
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
            campaign: CampaignResult) -> Path:
        root = Path(
            str(self.config.get("Simulation Output Directory") or
                "artifacts/echo_inventory/simulations")
        ).expanduser().resolve()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        target = root / stamp / "echo_enhancement_simulation.json"
        target.parent.mkdir(parents=True, exist_ok=False)
        target.write_text(
            json.dumps(
                self._simulation_report_data(source, campaign),
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
        campaign = StagedUpgradeCampaign(
            result.build,
            result.candidate_pool,
            tuners=tuners,
            echo_exp=tubes * 5_000,
            config=policy,
        ).run()
        for event in campaign.events:
            self._publish_simulation_event(event)

        report = self._write_simulation_report(result, campaign)
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
        return super().run()
