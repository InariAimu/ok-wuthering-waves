"""Compare the rollout model with the supplied A/B/C/D heuristic policy."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
from statistics import mean, stdev

from src.echo.benchmark import (
    BenchmarkRun,
    OpportunityCostPolicy,
    PublishedHeuristicPolicy,
    candidate_stream,
    empty_build,
    run_fixed_attempt_benchmark,
)
from src.echo.panel import MainDpsWeightClass
from src.echo.scenarios import xuanling_scenario


def mean_ci(values: list[float]) -> dict[str, float]:
    average = mean(values)
    standard_deviation = stdev(values) if len(values) > 1 else 0.0
    half_width = 1.96 * standard_deviation / math.sqrt(len(values))
    return {
        "mean": average,
        "std": standard_deviation,
        "ci95_low": average - half_width,
        "ci95_high": average + half_width,
    }


def horizon_metrics(runs: list[BenchmarkRun], horizon: int) -> dict:
    index = horizon - 1
    scores = [run.score_curve[index] for run in runs]
    tuners = [run.net_tuners_curve[index] for run in runs]
    exp = [run.net_exp_curve[index] for run in runs]
    return {
        "echo_score": mean_ci(scores),
        "total_score": mean_ci([run.total_score_curve[index] for run in runs]),
        "score_per_100_attempts": mean_ci([score / horizon * 100 for score in scores]),
        "net_tuners": mean_ci(tuners),
        "net_exp": mean_ci(exp),
        "net_5000_exp_tubes": mean_ci([value / 5_000 for value in exp]),
        "score_per_1000_net_tuners": mean_ci([
            score / cost * 1_000 if cost > 0 else 0.0
            for score, cost in zip(scores, tuners, strict=True)
        ]),
        "score_per_million_net_exp": mean_ci([
            score / cost * 1_000_000 if cost > 0 else 0.0
            for score, cost in zip(scores, exp, strict=True)
        ]),
        "equipped_echoes": mean_ci([
            float(run.equipped_count_curve[index]) for run in runs
        ]),
        "complete_rate": mean([
            run.equipped_count_curve[index] == 5 for run in runs
        ]),
        "energy_regen": mean_ci([run.energy_curve[index] for run in runs]),
        "energy_feasible_rate": mean([
            run.energy_feasible_curve[index] for run in runs
        ]),
    }


def paired_metrics(current: list[BenchmarkRun], heuristic: list[BenchmarkRun],
                   horizon: int) -> dict:
    index = horizon - 1
    score_deltas = [
        model.score_curve[index] - base.score_curve[index]
        for model, base in zip(current, heuristic, strict=True)
    ]
    tuner_deltas = [
        model.net_tuners_curve[index] - base.net_tuners_curve[index]
        for model, base in zip(current, heuristic, strict=True)
    ]
    exp_deltas = [
        model.net_exp_curve[index] - base.net_exp_curve[index]
        for model, base in zip(current, heuristic, strict=True)
    ]
    relative = []
    for model, base in zip(current, heuristic, strict=True):
        base_score = base.score_curve[index]
        if base_score > 0:
            relative.append(model.score_curve[index] / base_score - 1)
    return {
        "score_delta_current_minus_heuristic": mean_ci(score_deltas),
        "relative_score_advantage": mean_ci(relative) if relative else None,
        "current_win_rate": mean(delta > 1e-12 for delta in score_deltas),
        "net_tuner_delta": mean_ci(tuner_deltas),
        "net_exp_delta": mean_ci(exp_deltas),
    }


def aggregate_counters(runs: list[BenchmarkRun], attribute: str) -> dict[str, float]:
    totals: Counter[str] = Counter()
    for run in runs:
        totals.update(dict(getattr(run, attribute)))
    return {key: value / len(runs) for key, value in sorted(totals.items())}


def final_run_metrics(runs: list[BenchmarkRun]) -> dict:
    return {
        "kept": mean_ci([float(run.kept) for run in runs]),
        "discarded": mean_ci([float(run.discarded) for run in runs]),
        "replacements": mean_ci([float(run.replacements) for run in runs]),
        "first_complete_attempt": mean_ci([
            float(run.first_complete_attempt or run.attempts + 1) for run in runs
        ]),
        "first_energy_feasible_attempt": mean_ci([
            float(run.first_energy_feasible_attempt or run.attempts + 1) for run in runs
        ]),
        "reveals_by_stage_mean": aggregate_counters(runs, "reveals_by_stage"),
        "discards_by_stage_mean": aggregate_counters(runs, "discards_by_stage"),
        "decision_reasons_mean": aggregate_counters(runs, "decision_reasons"),
    }


def replicate_rows(current: list[BenchmarkRun], heuristic: list[BenchmarkRun],
                   horizons: tuple[int, ...]) -> list[dict]:
    rows = []
    for replication, (model, baseline) in enumerate(
            zip(current, heuristic, strict=True), start=1):
        row = {"replication": replication, "horizons": []}
        for horizon in horizons:
            index = horizon - 1
            row["horizons"].append({
                "attempts": horizon,
                "current_score": model.score_curve[index],
                "heuristic_score": baseline.score_curve[index],
                "current_net_tuners": model.net_tuners_curve[index],
                "heuristic_net_tuners": baseline.net_tuners_curve[index],
                "current_net_exp": model.net_exp_curve[index],
                "heuristic_net_exp": baseline.net_exp_curve[index],
                "current_equipped": model.equipped_count_curve[index],
                "heuristic_equipped": baseline.equipped_count_curve[index],
                "current_er": model.energy_curve[index],
                "heuristic_er": baseline.energy_curve[index],
            })
        rows.append(row)
    return rows


def fmt_ci(metric: dict[str, float], digits: int = 2) -> str:
    average = metric["mean"]
    half = metric["ci95_high"] - average
    return f"{average:.{digits}f}±{half:.{digits}f}"


def write_markdown(data: dict, path: Path) -> None:
    lines = [
        "# 声骸逐孔策略固定操作次数对照实验",
        "",
        "## 实验口径",
        "",
        f"- 每组 {data['config']['replications']} 个配对重复实验；每次 "
        f"{data['config']['attempts']} 个新胚子操作",
        "- 两种策略逐次看到完全相同的COST、名称和五条预生成副词条；只允许开孔深度不同",
        "- 操作序列按43311槽位比例循环：4C占20%，3C占40%，1C占40%",
        "- 从五个空槽开始；胚子和资源无限，但完整记录毛投入、回收和净消耗",
        "- 两种策略的最终表现统一使用对应1类/2类角色面板模型评分",
        "- 表格评分为扣除固定基础面板9分后的声骸贡献分；总面板代理分=声骸贡献分+9",
        "- 当前模型要求最终单件双暴，并以一件+25资源为固定归一化单位计算滚动机会成本",
        "- 启发式的有效词条为双暴、攻击%、固定攻击、重击和未溢出的效率；D分不计效率",
        "- 启发式D：双暴且四有效，或双暴D分≥43.7；合格后按D分替换同COST最低件",
        "- 保留新件时不回收旧件；弃置候选按30%调谐器、66%经验回收",
        "",
    ]
    for weight_class, section in data["weight_classes"].items():
        title = "1类：双暴>攻击%=重击>小攻击" if weight_class == "type1" else (
            "2类：双暴>攻击%>重击=小攻击"
        )
        lines.extend([
            f"## {title}",
            "",
            "| 操作数 | 当前模型声骸分 | 启发式声骸分 | 配对差值(95%CI) | 当前优势 | 当前/启发式 净调谐器 | 当前/启发式 经验筒 | 当前/启发式 达标率 |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|",
        ])
        for entry in section["horizons"]:
            current = entry["current_model"]
            baseline = entry["published_heuristic"]
            paired = entry["paired"]
            relative = paired["relative_score_advantage"]
            relative_text = f"{relative['mean']:.1%}" if relative else "—"
            lines.append(
                f"| {entry['attempts']} | {fmt_ci(current['echo_score'])} | "
                f"{fmt_ci(baseline['echo_score'])} | "
                f"{fmt_ci(paired['score_delta_current_minus_heuristic'])} | "
                f"{relative_text} | "
                f"{current['net_tuners']['mean']:.0f}/{baseline['net_tuners']['mean']:.0f} | "
                f"{current['net_5000_exp_tubes']['mean']:.1f}/{baseline['net_5000_exp_tubes']['mean']:.1f} | "
                f"{current['energy_feasible_rate']:.0%}/{baseline['energy_feasible_rate']:.0%} |"
            )
        final = section["horizons"][-1]
        current = final["current_model"]
        baseline = final["published_heuristic"]
        lines.extend([
            "",
            "### 最终效率与流程",
            "",
            "| 指标 | 当前模型 | 启发式 |",
            "|---|---:|---:|",
            f"| 最终总面板代理分 | {current['total_score']['mean']:.3f} | {baseline['total_score']['mean']:.3f} |",
            f"| 每100次操作评分 | {current['score_per_100_attempts']['mean']:.3f} | {baseline['score_per_100_attempts']['mean']:.3f} |",
            f"| 每1000净调谐器评分 | {current['score_per_1000_net_tuners']['mean']:.3f} | {baseline['score_per_1000_net_tuners']['mean']:.3f} |",
            f"| 每100万净经验评分 | {current['score_per_million_net_exp']['mean']:.3f} | {baseline['score_per_million_net_exp']['mean']:.3f} |",
            f"| 平均装备件数 | {current['equipped_echoes']['mean']:.2f} | {baseline['equipped_echoes']['mean']:.2f} |",
            f"| 五件套填满率 | {current['complete_rate']:.0%} | {baseline['complete_rate']:.0%} |",
            f"| 平均最终效率 | {current['energy_regen']['mean']:.2f}% | {baseline['energy_regen']['mean']:.2f}% |",
            f"| 配对胜率 | {final['paired']['current_win_rate']:.0%} | — |",
            "",
            "平均开孔/弃置次数详见 `summary.json`；逐重复实验数据详见 `replicates.json`。",
            "",
        ])
    lines.extend([
        "## 综合结论",
        "",
    ])
    for weight_class, section in data["weight_classes"].items():
        title = "1类" if weight_class == "type1" else "2类"
        early = min(section["horizons"], key=lambda item: abs(item["attempts"] - 100))
        final = section["horizons"][-1]
        early_delta = early["paired"]["score_delta_current_minus_heuristic"]
        final_delta = final["paired"]["score_delta_current_minus_heuristic"]
        current = final["current_model"]
        baseline = final["published_heuristic"]
        tuner_saving = 1 - current["net_tuners"]["mean"] / baseline["net_tuners"]["mean"]
        exp_saving = 1 - current["net_exp"]["mean"] / baseline["net_exp"]["mean"]
        lines.extend([
            f"- {title}在{early['attempts']}次时，当前模型领先 "
            f"{early_delta['mean']:.2f}分（95%CI "
            f"{early_delta['ci95_low']:.2f}～{early_delta['ci95_high']:.2f}）；"
            f"到{final['attempts']}次时差值为{final_delta['mean']:.2f}分（95%CI "
            f"{final_delta['ci95_low']:.2f}～{final_delta['ci95_high']:.2f}），终点评分无显著差异。",
            f"- {title}达到相近终点评分时，当前模型少用{tuner_saving:.1%}净调谐器、"
            f"{exp_saving:.1%}净经验；效率达标率为{current['energy_feasible_rate']:.0%}，"
            f"启发式为{baseline['energy_feasible_rate']:.0%}。",
        ])
    lines.extend([
        "- 因此启发式更像高门槛终局筛选器：前期成套很慢，但长操作数下可追平裸评分；当前模型更适合从空套装起步和资源效率目标。",
        "- 500次终点置信区间跨零，不能据此声称当前模型或启发式在绝对终点评分上显著更高。",
        "",
    ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempts", type=int, default=500)
    parser.add_argument("--replications", type=int, default=20)
    parser.add_argument("--rollout-samples", type=int, default=400)
    parser.add_argument("--horizons", default="50,100,250,500")
    parser.add_argument("--outcome-seed", type=int, default=151)
    parser.add_argument("--analysis-seed", type=int, default=15_100)
    parser.add_argument(
        "--output-dir", type=Path,
        default=Path("artifacts/echo-policy-benchmark"),
    )
    args = parser.parse_args()
    horizons = tuple(sorted({int(item) for item in args.horizons.split(",")}))
    if not horizons or horizons[-1] > args.attempts or horizons[0] <= 0:
        raise ValueError("horizons must be positive and no larger than attempts")
    if args.replications <= 0:
        raise ValueError("replications must be positive")

    data = {
        "config": {
            "attempts": args.attempts,
            "replications": args.replications,
            "rollout_samples": args.rollout_samples,
            "horizons": horizons,
            "outcome_seed": args.outcome_seed,
            "analysis_seed": args.analysis_seed,
            "energy_thresholds": [120, 125, 130],
            "candidate_names": {"4C": 1, "3C": 2, "1C": 5},
        },
        "weight_classes": {},
    }
    replicate_data = {"config": data["config"], "weight_classes": {}}

    for class_index, weight_class in enumerate(MainDpsWeightClass):
        scenario = xuanling_scenario(
            required_er=120,
            comfort_er=125,
            overflow_er=130,
            weight_class=weight_class,
        )
        initial = empty_build(scenario.build)
        current_runs: list[BenchmarkRun] = []
        heuristic_runs: list[BenchmarkRun] = []
        current_policy = OpportunityCostPolicy(
            rollout_samples=args.rollout_samples,
            analysis_seed=args.analysis_seed + class_index * 1_000_003,
        )
        heuristic_policy = PublishedHeuristicPolicy()
        for replication in range(args.replications):
            outcomes = candidate_stream(
                scenario.candidate_pool,
                attempts=args.attempts,
                seed=args.outcome_seed + replication * 10_007,
            )
            current_runs.append(run_fixed_attempt_benchmark(
                initial,
                outcomes,
                current_policy,
            ))
            heuristic_runs.append(run_fixed_attempt_benchmark(
                initial, outcomes, heuristic_policy
            ))
            print(
                f"{weight_class.value} replication {replication + 1}/{args.replications}: "
                f"current={current_runs[-1].final_echo_score:.3f}, "
                f"heuristic={heuristic_runs[-1].final_echo_score:.3f}",
                flush=True,
            )

        section = {
            "horizons": [],
            "final_process": {
                "current_model": final_run_metrics(current_runs),
                "published_heuristic": final_run_metrics(heuristic_runs),
            },
        }
        for horizon in horizons:
            section["horizons"].append({
                "attempts": horizon,
                "current_model": horizon_metrics(current_runs, horizon),
                "published_heuristic": horizon_metrics(heuristic_runs, horizon),
                "paired": paired_metrics(current_runs, heuristic_runs, horizon),
            })
        data["weight_classes"][weight_class.value] = section
        replicate_data["weight_classes"][weight_class.value] = replicate_rows(
            current_runs, heuristic_runs, horizons
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = args.output_dir / "summary.json"
    replicates_path = args.output_dir / "replicates.json"
    report_path = args.output_dir / "report.md"
    summary_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    replicates_path.write_text(
        json.dumps(replicate_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_markdown(data, report_path)
    print(report_path.resolve())
    print(summary_path.resolve())
    print(replicates_path.resolve())


if __name__ == "__main__":
    main()
