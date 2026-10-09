"""Run the reproducible Xuanling staged Echo-upgrade campaign."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.echo.optimizer import PolicyConfig, StagedUpgradeCampaign
from src.echo.panel import MainDpsWeightClass
from src.echo.reporting import write_json_report, write_markdown_report
from src.echo.scenarios import xuanling_scenario
from src.echo.simulation import EchoRoll, EchoState, EchoStat, ResourceCostModel


def operation_29_diagnostic(campaign: StagedUpgradeCampaign, output_dir: Path) -> None:
    """Evaluate the prior heavy/flat-ATK/ATK% +15 state under new constraints."""

    partial = EchoState((
        EchoRoll.from_value(EchoStat.HEAVY_ATTACK_DAMAGE, 7.9),
        EchoRoll.from_value(EchoStat.ATTACK_FLAT, 40),
        EchoRoll.from_value(EchoStat.ATTACK_PERCENT, 7.9),
    ))
    identity = next(
        item for item in campaign.candidate_pool if item.name.startswith("1C-C")
    )
    metrics = campaign.evaluate(identity, partial)
    _, baseline = max(
        campaign.fresh_options(),
        key=lambda item: (item[1].efficiency, item[1].expected_gain),
    )
    threshold = baseline.efficiency * campaign.config.continuation_efficiency_ratio
    missing = campaign.config.required_stats - partial.stats
    remaining_cost = ResourceCostModel().upgrade_cost(3, 5)
    should_continue = (
        len(missing) <= 2
        and metrics.success_probability > 0
        and metrics.efficiency >= threshold
    )
    data = {
        "partial_rolls": [
            {"stat": roll.stat.value, "value": roll.value} for roll in partial.rolls
        ],
        "missing_required_stats": sorted(stat.value for stat in missing),
        "remaining_slots": 2,
        "exact_probability_last_two_types_are_double_crit": 1 / 45,
        "gross_remaining_tuners": remaining_cost.tuners,
        "gross_remaining_exp": remaining_cost.echo_exp,
        "rollout_success_probability": metrics.success_probability,
        "rollout_expected_gain": metrics.expected_gain,
        "rollout_expected_gain_on_success": metrics.expected_gain_on_success,
        "expected_relative_tuners_vs_discard_now": metrics.expected_relative_tuners,
        "expected_relative_exp_vs_discard_now": metrics.expected_relative_exp,
        "conditioned_efficiency": metrics.efficiency,
        "fresh_baseline_efficiency": baseline.efficiency,
        "continue_threshold": threshold,
        "decision": "continue" if should_continue else "discard",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "operation29_evaluation.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# 原第29次操作专项评估",
        "",
        "当前+15词条：重击7.9、固定攻击40、攻击7.9，剩余两孔且双爆均未出现。",
        "",
        f"- 后两孔类型恰为双爆的精确概率：{1 / 45:.4%}",
        f"- 条件模拟成功概率：{metrics.success_probability:.4%}",
        f"- 条件期望提升：{metrics.expected_gain:.6f}",
        f"- 成功时平均提升：{metrics.expected_gain_on_success:.6f}",
        f"- 相对立即弃置的期望净成本：调谐器 {metrics.expected_relative_tuners:.4f}，"
        f"经验 {metrics.expected_relative_exp:.2f}",
        f"- 当前完成效率：{metrics.efficiency:.6f}",
        f"- 新胚子基准效率：{baseline.efficiency:.6f}",
        f"- 继续阈值：{threshold:.6f}",
        f"- 决策：{'继续' if should_continue else '弃置'}",
        "",
    ]
    (output_dir / "operation29_evaluation.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=4_000)
    parser.add_argument("--heavy-coverage", type=float, default=0.75)
    parser.add_argument(
        "--weight-class", choices=("coverage", "type1", "type2"),
        default="coverage",
    )
    parser.add_argument("--require-double-crit", action="store_true")
    parser.add_argument("--required-er", type=float, default=130)
    parser.add_argument("--comfort-er", type=float, default=135)
    parser.add_argument("--overflow-er", type=float, default=145)
    parser.add_argument("--analysis-seed", type=int, default=15_100)
    parser.add_argument("--outcome-seed", type=int, default=151)
    parser.add_argument("--max-attempts", type=int, default=2_000)
    parser.add_argument(
        "--output-dir", type=Path,
        default=Path("artifacts/xuanling-echo-simulation"),
    )
    args = parser.parse_args()

    scenario = xuanling_scenario(
        heavy_damage_coverage=args.heavy_coverage,
        required_er=args.required_er,
        comfort_er=args.comfort_er,
        overflow_er=args.overflow_er,
        weight_class=(
            None if args.weight_class == "coverage"
            else MainDpsWeightClass(args.weight_class)
        ),
    )
    required_stats = (
        frozenset({EchoStat.CRIT_RATE, EchoStat.CRIT_DAMAGE})
        if args.require_double_crit else frozenset()
    )
    policy = PolicyConfig(
        rollout_samples=args.samples,
        max_attempts=args.max_attempts,
        analysis_seed=args.analysis_seed,
        outcome_seed=args.outcome_seed,
        required_stats=required_stats,
    )
    diagnostic_campaign = StagedUpgradeCampaign(
        scenario.build,
        scenario.candidate_pool,
        tuners=6_621,
        echo_exp=5_000 * 1_275,
        config=policy,
    )
    operation_29_diagnostic(diagnostic_campaign, args.output_dir)
    campaign = StagedUpgradeCampaign(
        scenario.build,
        scenario.candidate_pool,
        tuners=6_621,
        echo_exp=5_000 * 1_275,
        config=policy,
    )
    result = campaign.run()

    markdown_path = args.output_dir / "report.md"
    json_path = args.output_dir / "report.json"
    write_markdown_report(result, scenario, markdown_path)
    write_json_report(result, scenario, json_path)
    print(f"attempts={result.attempts}")
    print(f"successes={result.successes}")
    print(f"failures={result.failures}")
    print(f"score={result.initial_build.score:.4f}->{result.final_build.score:.4f}")
    print(f"tuners={result.ledger.initial_tuners:.0f}->{result.ledger.tuners:.1f}")
    print(f"exp={result.ledger.initial_exp:.0f}->{result.ledger.echo_exp:.0f}")
    print(f"stop={result.stop_reason}")
    print(markdown_path.resolve())
    print(json_path.resolve())


if __name__ == "__main__":
    main()
