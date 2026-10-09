"""Run step two in guaranteed offline simulation mode.

This harness records every value that the task would publish to the OK-WW
task info panel, plus every task log line. It never creates a game runtime.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.task.ModelEnhanceEchoTask import ModelEnhanceEchoTask


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("check", type=Path, help="reviewed echo_inventory_check.json")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/echo_inventory/simulations"),
    )
    parser.add_argument("--attempts", type=int, default=20)
    parser.add_argument("--samples", type=int, default=300)
    parser.add_argument("--tuners", type=float, default=1000)
    parser.add_argument("--premium-tubes", type=float, default=300)
    return parser.parse_args()


def make_offline_task(args: argparse.Namespace) -> ModelEnhanceEchoTask:
    task = ModelEnhanceEchoTask.__new__(ModelEnhanceEchoTask)
    task._echo_enhancement_session = None
    task._echo_enhancement_context = None
    task._echo_candidate_provider = None
    task._session_is_external = False
    task._selected_sonata_set = None
    task._loaded_inspection_result = None
    task.config = {
        "Inspection Result File": str(args.check.resolve()),
        "Simulation Only": True,
        "Simulation Attempts": args.attempts,
        "Simulation Rollout Samples": args.samples,
        "Simulation Tuners": args.tuners,
        "Simulation Premium Echo EXP Tubes": args.premium_tubes,
        "Simulation Output Directory": str(args.output_dir.resolve()),
    }
    task.offline_info = {}
    task.offline_info_history = []
    task.offline_logs = []

    def info_set(key, value):
        task.offline_info[key] = value
        task.offline_info_history.append({"key": key, "value": value})

    task.info_set = info_set
    task.log_info = lambda message, **kwargs: task.offline_logs.append(str(message))

    def reject_game_action(*args, **kwargs):
        raise AssertionError("offline simulation attempted to touch the game UI")

    task.select_echo_sonata_set = reject_game_action
    return task


def main() -> None:
    args = parse_args()
    task = make_offline_task(args)
    campaign = task.run()
    report = Path(task.offline_info["模拟报告"])
    trace = report.with_name("echo_enhancement_ui_trace.json")
    trace.write_text(
        json.dumps(
            {
                "kind": "ok-ww.echo-enhancement-ui-trace",
                "simulation_report": str(report),
                "final_info": task.offline_info,
                "info_history": task.offline_info_history,
                "logs": task.offline_logs,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps({
        "attempts": campaign.attempts,
        "successes": campaign.successes,
        "failures": campaign.failures,
        "initial_score": campaign.initial_build.score,
        "final_score": campaign.final_build.score,
        "event_count": len(campaign.events),
        "info_update_count": len(task.offline_info_history),
        "log_count": len(task.offline_logs),
        "report": str(report),
        "ui_trace": str(trace),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
