"""Measure whether a high first Crit roll should be committed directly to +25."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from statistics import mean, stdev

from src.echo.benchmark import (
    FirstCritCommitPolicy,
    OpportunityCostPolicy,
    candidate_stream,
    empty_build,
    run_fixed_attempt_benchmark,
)
from src.echo.panel import MainDpsWeightClass
from src.echo.scenarios import xuanling_scenario
from src.echo.simulation import EchoStat, ROLL_TABLE


def mean_ci(values: list[float]) -> dict[str, float]:
    average = mean(values)
    deviation = stdev(values) if len(values) > 1 else 0.0
    half = 1.96 * deviation / math.sqrt(len(values))
    return {
        "mean": average,
        "ci95_low": average - half,
        "ci95_high": average + half,
    }


def trigger_probability(equivalent_threshold: float) -> float:
    probability = 0.0
    for stat in (EchoStat.CRIT_RATE, EchoStat.CRIT_DAMAGE):
        tiers = ROLL_TABLE[stat]
        total = sum(tier.weight for tier in tiers)
        qualifying = sum(
            tier.weight for tier in tiers
            if tier.value * (2 if stat == EchoStat.CRIT_RATE else 1)
            >= equivalent_threshold - 1e-12
        )
        probability += (1 / 13) * qualifying / total
    return probability


def run_metrics(runs, horizon: int) -> dict:
    index = horizon - 1
    return {
        "score": mean_ci([run.score_curve[index] for run in runs]),
        "net_tuners": mean_ci([run.net_tuners_curve[index] for run in runs]),
        "net_exp_tubes": mean_ci([
            run.net_exp_curve[index] / 5_000 for run in runs
        ]),
        "energy_feasible_rate": mean([
            run.energy_feasible_curve[index] for run in runs
        ]),
    }


def paired_metrics(override_runs, standard_runs, horizon: int) -> dict:
    index = horizon - 1
    return {
        "score_delta": mean_ci([
            override.score_curve[index] - standard.score_curve[index]
            for override, standard in zip(override_runs, standard_runs, strict=True)
        ]),
        "additional_net_tuners": mean_ci([
            override.net_tuners_curve[index] - standard.net_tuners_curve[index]
            for override, standard in zip(override_runs, standard_runs, strict=True)
        ]),
        "additional_net_exp_tubes": mean_ci([
            (override.net_exp_curve[index] - standard.net_exp_curve[index]) / 5_000
            for override, standard in zip(override_runs, standard_runs, strict=True)
        ]),
    }


def write_report(data: dict, path: Path) -> None:
    config = data["config"]
    lines = [
        "# 首孔高双暴词条直接+25专项实验",
        "",
        "## 口径",
        "",
        f"- 每类{config['replications']}组配对实验，每组{config['attempts']}个胚子；"
        f"逐孔模型使用{config['rollout_samples']}次条件抽样",
        "- 暴击率按2倍换算，因此9.9暴击与19.8爆伤属于同一阈值",
        "- 触发后从+5直接开到+25，不再执行+10/+15/+20中途止损；最终仍要求双暴并能提升套装",
        "- 未触发的胚子完全使用当前逐孔机会成本模型",
        "- 两策略逐次看到相同的胚子和完整词条，差值是配对差值",
        "",
        "首孔已经是暴击类后，余下四孔包含另一种暴击类的结构概率为4/12=33.3333%。",
        "",
    ]
    for weight_class, section in data["weight_classes"].items():
        lines.extend([
            f"## {'1类' if weight_class == 'type1' else '2类'}",
            "",
            "| 换算阈值 | 单次触发率 | 500次平均触发 | 操作数 | 评分增量 | 额外净调谐器 | 额外经验筒 |",
            "|---:|---:|---:|---:|---:|---:|---:|",
        ])
        for threshold in section["thresholds"]:
            for horizon in threshold["horizons"]:
                paired = horizon["paired"]
                delta = paired["score_delta"]
                lines.append(
                    f"| {threshold['equivalent_threshold']:g} | "
                    f"{threshold['trigger_probability']:.4%} | "
                    f"{threshold['mean_triggers']:.2f} | {horizon['attempts']} | "
                    f"{delta['mean']:.3f} [{delta['ci95_low']:.3f}, {delta['ci95_high']:.3f}] | "
                    f"{paired['additional_net_tuners']['mean']:.1f} | "
                    f"{paired['additional_net_exp_tubes']['mean']:.2f} |"
                )
        lines.append("")
    lines.extend([
        "## 判定",
        "",
        "若评分增量的95%置信区间跨零，同时额外资源为正，则没有证据支持用盲拉替代逐孔判断。",
        "逐孔判断并不会丢失高值词条本身；它只会在后续组合已不值得时行使弃置的选择权。",
        "",
    ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempts", type=int, default=500)
    parser.add_argument("--replications", type=int, default=20)
    parser.add_argument("--rollout-samples", type=int, default=200)
    parser.add_argument("--horizons", default="100,250,500")
    parser.add_argument("--thresholds", default="18.6,19.8,21")
    parser.add_argument("--outcome-seed", type=int, default=151)
    parser.add_argument("--analysis-seed", type=int, default=15_100)
    parser.add_argument(
        "--output-dir", type=Path,
        default=Path("artifacts/first-crit-commit"),
    )
    args = parser.parse_args()
    horizons = tuple(sorted({int(value) for value in args.horizons.split(",")}))
    thresholds = tuple(sorted({float(value) for value in args.thresholds.split(",")}))
    if not horizons or horizons[-1] > args.attempts:
        raise ValueError("horizons must not exceed attempts")

    data = {
        "config": {
            "attempts": args.attempts,
            "replications": args.replications,
            "rollout_samples": args.rollout_samples,
            "horizons": horizons,
            "thresholds": thresholds,
            "double_crit_completion_probability_after_first_crit": 1 / 3,
        },
        "weight_classes": {},
    }
    for class_index, weight_class in enumerate(MainDpsWeightClass):
        scenario = xuanling_scenario(
            required_er=120,
            comfort_er=125,
            overflow_er=130,
            weight_class=weight_class,
        )
        initial = empty_build(scenario.build)
        delegate = OpportunityCostPolicy(
            rollout_samples=args.rollout_samples,
            analysis_seed=args.analysis_seed + class_index * 1_000_003,
        )
        standard_runs = []
        override_runs = {threshold: [] for threshold in thresholds}
        for replication in range(args.replications):
            outcomes = candidate_stream(
                scenario.candidate_pool,
                attempts=args.attempts,
                seed=args.outcome_seed + replication * 10_007,
            )
            standard_runs.append(run_fixed_attempt_benchmark(
                initial, outcomes, delegate
            ))
            for threshold in thresholds:
                override_runs[threshold].append(run_fixed_attempt_benchmark(
                    initial,
                    outcomes,
                    FirstCritCommitPolicy(
                        delegate, equivalent_threshold=threshold
                    ),
                ))
            print(
                f"{weight_class.value} {replication + 1}/{args.replications}",
                flush=True,
            )

        section = {
            "standard": {
                "horizons": [
                    {"attempts": horizon, **run_metrics(standard_runs, horizon)}
                    for horizon in horizons
                ]
            },
            "thresholds": [],
        }
        for threshold in thresholds:
            runs = override_runs[threshold]
            trigger_counts = [
                dict(run.decision_reasons).get("first_crit_commit", 0)
                for run in runs
            ]
            section["thresholds"].append({
                "equivalent_threshold": threshold,
                "crit_rate_threshold": threshold / 2,
                "crit_damage_threshold": threshold,
                "trigger_probability": trigger_probability(threshold),
                "mean_triggers": mean(trigger_counts),
                "horizons": [
                    {
                        "attempts": horizon,
                        "override": run_metrics(runs, horizon),
                        "paired": paired_metrics(runs, standard_runs, horizon),
                    }
                    for horizon in horizons
                ],
            })
        data["weight_classes"][weight_class.value] = section

    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "summary.json"
    report_path = args.output_dir / "report.md"
    json_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_report(data, report_path)
    print(report_path.resolve())
    print(json_path.resolve())


if __name__ == "__main__":
    main()
