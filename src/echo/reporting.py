"""Serialization and Markdown rendering for staged Echo simulations."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
import json
from pathlib import Path

from src.echo.optimizer import CampaignEvent, CampaignResult, NamedEchoBuild
from src.echo.scenarios import Scenario
from src.echo.simulation import EchoStat, ResourceCostModel


def _round(value: float, digits: int = 4) -> float:
    return round(value, digits)


def _build_data(build: NamedEchoBuild) -> dict:
    return {
        "score": build.score,
        "energy_regen": build.panel.value(EchoStat.ENERGY_REGEN),
        "echoes": [
            {
                "slot": echo.slot,
                "name": echo.identity.name,
                "cost": echo.identity.cost,
                "compatibility": echo.identity.compatibility,
                "rolls": [
                    {"stat": roll.stat.value, "value": roll.value, "tier": roll.tier}
                    for roll in echo.state.rolls
                ],
            }
            for echo in build.echoes
        ],
    }


def campaign_data(result: CampaignResult, scenario: Scenario) -> dict:
    ledger = result.ledger
    return {
        "scenario": scenario.name,
        "assumptions": list(scenario.assumptions),
        "policy": {
            **{
                key: value for key, value in asdict(result.config).items()
                if key != "required_stats"
            },
            "required_stats": sorted(stat.value for stat in result.config.required_stats),
        },
        "summary": {
            "attempts": result.attempts,
            "successes": result.successes,
            "failures": result.failures,
            "stop_reason": result.stop_reason,
            "initial_score": result.initial_build.score,
            "final_score": result.final_build.score,
            "score_gain": result.final_build.score - result.initial_build.score,
        },
        "resources": {
            "initial_tuners": ledger.initial_tuners,
            "initial_exp": ledger.initial_exp,
            "initial_premium_tubes": ledger.initial_exp / 5_000,
            "remaining_tuners": ledger.tuners,
            "remaining_exp": ledger.echo_exp,
            "remaining_premium_tubes": ledger.echo_exp / 5_000,
            "gross_tuners": ledger.gross_tuners,
            "gross_exp": ledger.gross_exp,
            "refunded_tuners": ledger.refunded_tuners,
            "refunded_exp": ledger.refunded_exp,
            "net_tuners": ledger.net_tuners,
            "net_exp": ledger.net_exp,
            "net_premium_tubes": ledger.net_exp / 5_000,
        },
        "initial_build": _build_data(result.initial_build),
        "final_build": _build_data(result.final_build),
        "events": [asdict(event) for event in result.events],
    }


def write_json_report(result: CampaignResult, scenario: Scenario, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(campaign_data(result, scenario), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _resource_text(resources: dict[str, float]) -> str:
    return (
        f"T={resources['tuners']:.1f}, "
        f"EXP={resources['echo_exp']:.0f} ({resources['premium_tubes']:.2f}筒)"
    )


def _event_detail(event: CampaignEvent) -> str:
    details = event.details
    if event.action == "continue":
        metrics = details["current_metrics_before_reveal"]
        return (
            f"开出 `{details['revealed_stat']}={details['revealed_value']}`；"
            f"开孔前 p={metrics['success_probability']:.2%}，"
            f"期望提升={metrics['expected_gain']:.4f}，"
            f"效率={metrics['efficiency']:.4f}"
        )
    if event.action == "replace_success":
        return (
            f"替换 `{details['slot']}` 的 `{details['replaced_name']}`；"
            f"提升={details['score_gain']:.4f}；"
            f"词条={details['rolls']}"
        )
    if event.action.startswith("discard"):
        metrics = details.get("current_metrics")
        metric_text = ""
        if metrics:
            metric_text = (
                f"p={metrics['success_probability']:.2%}，"
                f"期望提升={metrics['expected_gain']:.4f}；"
            )
        if details.get("deferred_recycling"):
            refund_text = "本轮仅标记弃置，不返还资源；"
        else:
            refund_text = (
                f"返还T={details.get('refund_tuners', 0):.1f}, "
                f"EXP={details.get('refund_exp', 0):.0f}；"
            )
        return f"{metric_text}{refund_text}词条={details.get('rolls', [])}"
    if event.action == "start":
        metrics = details["fresh_metrics"]
        return (
            f"可替换={details['compatible_slots']}；"
            f"p={metrics['success_probability']:.2%}，"
            f"期望提升={metrics['expected_gain']:.4f}，"
            f"效率={metrics['efficiency']:.4f}"
        )
    return ""


def _reason_zh(reason: str) -> str:
    exact = {
        "maximum attempts reached": "达到最大尝试次数",
        "resources cannot pay for the first +5 reveal": "剩余资源无法支付第一次+5开孔",
        "resources cannot pay for the next staged reveal": "剩余资源无法支付下一阶段开孔",
        "selected the fresh candidate with the highest expected gain per limiting-resource fraction":
            "选择单位瓶颈资源期望提升最高的新胚子",
        "candidate is the best valid replacement among all compatible same-COST slots":
            "候选是全部同COST兼容位置中的最优有效替换",
        "full candidate has no energy-feasible replacement above the minimum gain":
            "满级候选没有满足效率硬约束及最低提升的替换位置",
        "conditioned completion efficiency fell below the fresh-candidate opportunity cost":
            "当前半成品的条件完成效率低于新胚子机会成本",
        "no sampled valid completion remains": "抽样中已不存在有效完成结果",
        "remaining slots cannot satisfy all required substats": "剩余孔数不足以满足全部必需词条",
        "conditioned completion efficiency meets the rolling fresh-candidate benchmark":
            "当前半成品的条件完成效率达到滚动新胚子基准",
    }
    if reason in exact:
        return exact[reason]
    if reason.startswith("no sampled fresh candidate can produce"):
        return "抽样中已没有新胚子能够产生有效严格提升"
    return reason


def render_markdown(result: CampaignResult, scenario: Scenario) -> str:
    ledger = result.ledger
    action_counts = Counter(event.action for event in result.events)
    discard_levels = Counter(
        event.level for event in result.events if event.action.startswith("discard")
    )
    reveal_levels = Counter(
        event.level for event in result.events if event.action == "continue"
    )
    costs = ResourceCostModel()
    if result.config.deferred_recycling:
        direct_full = costs.invested_cost(5) * result.attempts
    else:
        direct_full = (
            costs.attempt_cost(5, keep=True) * result.successes
            + costs.attempt_cost(5, keep=False) * result.failures
        )
    tuner_saving = (
        1 - ledger.net_tuners / direct_full.tuners if direct_full.tuners else 0
    )
    exp_saving = 1 - ledger.net_exp / direct_full.echo_exp if direct_full.echo_exp else 0
    lines = [
        f"# {scenario.name}逐孔强化模拟报告",
        "",
        "## 结论摘要",
        "",
        f"- 尝试：{result.attempts}；成功替换：{result.successes}；失败/弃置：{result.failures}",
        f"- 代理分：{result.initial_build.score:.4f} → {result.final_build.score:.4f}，"
        f"总提升 {result.final_build.score - result.initial_build.score:.4f}",
        f"- 调谐器：{ledger.initial_tuners:.0f} → {ledger.tuners:.1f}，净消耗 {ledger.net_tuners:.1f}",
        f"- 经验：{ledger.initial_exp:.0f} → {ledger.echo_exp:.0f}，"
        f"净消耗 {ledger.net_exp:.0f}（{ledger.net_exp / 5_000:.2f} 个5000经验筒）",
        f"- 停止原因：{_reason_zh(result.stop_reason)}",
        f"- 相对“{result.attempts}个胚子全部拉到+25、保持相同成败数量”的成本基线："
        f"节省调谐器 {tuner_saving:.2%}、经验 {exp_saving:.2%}",
        "",
        "## 模拟假设",
        "",
    ]
    lines.extend(f"- {assumption}" for assumption in scenario.assumptions)
    required_text = ", ".join(
        sorted(stat.value for stat in result.config.required_stats)
    ) or "无"
    lines.extend([
        f"- 评估抽样：每个状态 {result.config.rollout_samples} 次；"
        f"继续效率容差={result.config.continuation_efficiency_ratio:.0%}",
        f"- 候选必需副词条：{required_text}",
        (
            "- 弃置结算：本轮只在游戏中标记弃置，不立即分解或返还资源。"
            if result.config.deferred_recycling
            else "- 弃置结算：模拟中立即计入可回收调谐器与经验。"
        ),
        "- 策略是滚动机会成本近似，不是完整五槽双资源MDP的全局最优证明。",
        "",
        "## 资源账本",
        "",
        "| 项目 | 调谐器 | 经验 | 5000经验筒 |",
        "|---|---:|---:|---:|",
        f"| 初始 | {ledger.initial_tuners:.1f} | {ledger.initial_exp:.0f} | {ledger.initial_exp / 5_000:.2f} |",
        f"| 毛投入 | {ledger.gross_tuners:.1f} | {ledger.gross_exp:.0f} | {ledger.gross_exp / 5_000:.2f} |",
        f"| 回收 | {ledger.refunded_tuners:.1f} | {ledger.refunded_exp:.0f} | {ledger.refunded_exp / 5_000:.2f} |",
        f"| 净消耗 | {ledger.net_tuners:.1f} | {ledger.net_exp:.0f} | {ledger.net_exp / 5_000:.2f} |",
        f"| 剩余 | {ledger.tuners:.1f} | {ledger.echo_exp:.0f} | {ledger.echo_exp / 5_000:.2f} |",
        "",
        "## 分阶段结果",
        "",
        "| 阶段 | 实际开孔次数 | 在该阶段弃置 |",
        "|---|---:|---:|",
    ])
    for level in (5, 10, 15, 20, 25):
        lines.append(f"| +{level} | {reveal_levels[level]} | {discard_levels[level]} |")
    lines.extend([
        "",
        f"满级成功替换 {action_counts['replace_success']} 次；满级失败弃置 "
        f"{sum(1 for event in result.events if event.action.startswith('discard') and event.level == 25)} 次。",
        "",
        "## 最终套装",
        "",
        "| 槽位 | 名称 | COST | 副词条 |",
        "|---|---|---:|---|",
    ])
    for echo in result.final_build.echoes:
        rolls = ", ".join(f"{roll.stat.value}={roll.value}" for roll in echo.state.rolls)
        lines.append(f"| {echo.slot} | {echo.identity.name} | {echo.identity.cost} | {rolls} |")

    lines.extend([
        "",
        "## 完整操作记录",
        "",
        "| # | 尝试 | 操作 | 候选 | 等级 | 决策依据/结果 | 资源变化 |",
        "|---:|---:|---|---|---:|---|---|",
    ])
    action_names = {
        "start": "开始",
        "continue": "继续",
        "discard_failure": "失败/弃置",
        "discard_resource_stop": "资源不足/弃置",
        "replace_success": "成功/替换",
    }
    for event in result.events:
        before = _resource_text(event.resources_before)
        after = _resource_text(event.resources_after)
        reason = f"{_reason_zh(event.reason)}；{_event_detail(event)}"
        lines.append(
            f"| {event.index} | {event.attempt} | {action_names.get(event.action, event.action)} "
            f"| {event.candidate or '-'} | {event.level if event.level is not None else '-'} "
            f"| {reason} | {before} → {after} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_markdown_report(result: CampaignResult, scenario: Scenario, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(result, scenario), encoding="utf-8")
