"""Run one bounded, real step-two Echo enhancement candidate.

Unlike the read-only feasibility probe, this script can spend in-game Echo
EXP/tuners and can mark failed candidates as discarded or lock successes.
It defaults to one candidate, supports an explicit bounded multi-candidate
limit for continuation tests, and writes a structured trace.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sys
import time
import traceback

import cv2

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from config import config as project_config
from ok import OK
from src.task.ModelEnhanceEchoTask import ModelEnhanceEchoTask


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("inspection_result", type=Path)
    parser.add_argument("--tuners", type=int, default=50)
    parser.add_argument("--echo-exp", type=int, default=142_600)
    parser.add_argument("--max-candidates", type=int, default=1)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--settle", type=float, default=1.5)
    parser.add_argument(
        "--recover-discard-current",
        action="store_true",
        help="mark the currently open failed candidate discarded and return",
    )
    parser.add_argument(
        "--diagnose-status-only",
        action="store_true",
        help="read lock/discard template scores without changing state",
    )
    return parser.parse_args()


def json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return str(value)


def save_webp(path: Path, frame) -> None:
    encoded, data = cv2.imencode(
        ".webp",
        frame,
        [cv2.IMWRITE_WEBP_QUALITY, 62],
    )
    if not encoded:
        raise RuntimeError(f"failed to encode final WebP: {path}")
    path.write_bytes(data.tobytes())


def main() -> int:
    args = parse_args()
    if args.max_candidates <= 0:
        raise ValueError("--max-candidates must be positive")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = (
        args.output
        or Path("artifacts/echo_inventory/live_enhancement_tests") / timestamp
    ).resolve()
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / "live_echo_enhancement_test.json"

    config = deepcopy(project_config)
    config["check_mutex"] = False
    config["gui"] = None
    config["custom_tasks"] = False
    config["config_folder"] = str(output / "configs")
    config["screenshots_folder"] = str(output / "screenshots")
    config["log_file"] = str(output / "ok-ww.log")
    config["error_log_file"] = str(output / "ok-ww_error.log")
    config["onetime_tasks"] = [[
        ModelEnhanceEchoTask.__module__,
        ModelEnhanceEchoTask.__name__,
    ]]
    config["trigger_tasks"] = []
    config["windows"] = deepcopy(config["windows"])
    config["windows"]["start_exe"] = False
    config["windows"]["interaction"] = "PostMessage"

    runtime = OK(config)
    task = None
    logs: list[dict[str, str]] = []
    error = None
    started_at = datetime.now().astimezone().isoformat()
    try:
        runtime.start_runtime()
        deadline = time.time() + 15
        while time.time() < deadline:
            method = runtime.task_executor.method
            if method is not None and method.connected():
                break
            runtime.device_manager.refresh()
            time.sleep(0.5)
        else:
            raise RuntimeError("ok-ww could not connect to the running game window")

        runtime.task_executor.paused = False
        task = runtime.get_onetime_task(ModelEnhanceEchoTask)
        task.config["Inspection Result File"] = str(
            args.inspection_result.resolve()
        )
        task.config["Simulation Only"] = False
        task.config["Pause after Success"] = False
        task.config["Resource Source"] = "Manual"
        task.config["Manual Tuners"] = args.tuners
        task.config["Manual Premium Echo EXP Tubes"] = 0
        task.config["Manual Echo EXP"] = args.echo_exp
        task.config["Max Candidates Per Run"] = args.max_candidates

        original_info = task.log_info
        original_warning = task.log_warning
        original_error = task.log_error

        def record_info(message, *call_args, **call_kwargs):
            logs.append({"level": "info", "message": str(message)})
            return original_info(message, *call_args, **call_kwargs)

        def record_warning(message, *call_args, **call_kwargs):
            logs.append({"level": "warning", "message": str(message)})
            return original_warning(message, *call_args, **call_kwargs)

        def record_error(message, *call_args, **call_kwargs):
            logs.append({"level": "error", "message": str(message)})
            return original_error(message, *call_args, **call_kwargs)

        task.log_info = record_info
        task.log_warning = record_warning
        task.log_error = record_error
        if args.diagnose_status_only:
            status_box = task._echo_status_feature_box((
                "echo_dropped",
                "echo_not_dropped",
                "echo_locked",
                "echo_not_locked",
            ))
            task.info_set("状态模板框", str(status_box))
            for threshold in (0.1, 0.3, 0.5, 0.7):
                match = task.find_best_match_in_box(
                    status_box,
                    [
                        "echo_dropped",
                        "echo_not_dropped",
                        "echo_locked",
                        "echo_not_locked",
                    ],
                    threshold=threshold,
                )
                task.info_set(f"状态模板阈值{threshold}", str(match) if match else None)
        elif args.recover_discard_current:
            task.info_set("成功声骸数量", 0)
            task.info_set("失败声骸数量", 0)
            task.fail_reason = "实机测试恢复_模型已判定弃置"
            task.mark_discarded_and_esc()
        else:
            task.run()
    except Exception as caught:
        error = {
            "type": type(caught).__name__,
            "message": str(caught),
            "traceback": traceback.format_exc(),
        }
    finally:
        if task is not None and task.executor.method is not None:
            frame = task.executor.method.get_frame()
            if frame is not None:
                save_webp(output / "final_state.webp", frame)
        report = {
            "kind": "ok-ww.live-echo-enhancement-test",
            "started_at": started_at,
            "finished_at": datetime.now().astimezone().isoformat(),
            "inspection_result": str(args.inspection_result.resolve()),
            "mode": (
                "diagnose_status_only"
                if args.diagnose_status_only
                else (
                    "recover_discard_current"
                    if args.recover_discard_current
                    else "bounded_live_enhancement"
                )
            ),
            "safety_limit": {"max_candidates": args.max_candidates},
            "manual_budget": {
                "tuners": args.tuners,
                "echo_exp": args.echo_exp,
            },
            "completed": error is None,
            "task_info": json_safe(task.info if task is not None else {}),
            "logs": logs,
            "error": error,
            "final_state_webp": str(output / "final_state.webp"),
        }
        cleanup_errors = []
        for name, cleanup in (
            ("runtime.quit", runtime.quit),
            ("runtime.wait_task", runtime.wait_task),
            ("device_manager.close", runtime.device_manager.close),
        ):
            try:
                cleanup()
            except BaseException as cleanup_error:
                cleanup_errors.append({
                    "step": name,
                    "type": type(cleanup_error).__name__,
                    "message": str(cleanup_error),
                })
        report["cleanup_errors"] = cleanup_errors
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    print(report_path)
    return 0 if error is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
