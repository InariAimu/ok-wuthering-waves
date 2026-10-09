"""Read-only live probe for the Echo inventory page.

The probe reuses ok-script's configured window capture, PostMessage input and
OCR implementation.  It records a PNG and JSON OCR output before and after an
optional navigation click.  It never clicks enhancement, lock, discard,
dismantle or confirmation controls.
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
from src.echo import EchoEnhancementSession, EchoInventoryCheckResult, EchoResourceSnapshot
from src.echo.optimizer import EchoIdentity, NamedEchoBuild, NamedEquippedEcho, PolicyConfig
from src.echo.scenarios import xuanling_scenario
from src.task.CheckEchoInventoryTask import CheckEchoInventoryTask
from src.task.EnhanceEchoTask import EnhanceEchoTask
from src.task.ModelEnhanceEchoTask import ModelEnhanceEchoTask


SAFE_CLICK_ZONES = {
    "equipped_4c": (0.064, 0.235),
    "equipped_3c_1": (0.064, 0.400),
    "equipped_3c_2": (0.064, 0.500),
    "equipped_1c_1": (0.064, 0.600),
    "equipped_1c_2": (0.064, 0.700),
    "sonata_filter": (0.100, 0.040),
    "sonata_role_set": (0.195, 0.258),
    "cost_1": (0.191, 0.074),
    "cost_3": (0.239, 0.074),
    "cost_4": (0.288, 0.074),
    "sort_filter": (0.205, 0.925),
    "sort_direction": (0.300, 0.925),
    # Input coordinates exclude the 30px BitBlt title-bar offset.
    "sort_unequipped": (0.245, 0.300),
    "sort_level": (0.440, 0.300),
    "sort_confirm": (0.805, 0.840),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="observe")
    parser.add_argument("--click-zone", choices=sorted(SAFE_CLICK_ZONES))
    parser.add_argument(
        "--sequence",
        choices=[
            "equipped",
            "select_role_set",
            "select_sonata",
            "diagnose_sonata",
            "cost_tabs",
            "inventory",
            "inspect_candidate",
            "inspect_model_candidate",
            "prepare_enhancement_sort",
            "check",
            "escape",
            "bag",
        ],
    )
    parser.add_argument(
        "--sonata-set",
        help="exact Sonata set name used by the select_sonata sequence",
    )
    parser.add_argument(
        "--inspection-result",
        type=Path,
        help="reviewed step-one JSON used by inspect_model_candidate",
    )
    parser.add_argument(
        "--costs",
        default="1,3,4",
        help="comma-separated COST tabs used by the inventory sequence",
    )
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument(
        "--include-incomplete",
        action="store_true",
        help="include Echoes below +25 instead of scanning completed Echoes only",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--settle",
        type=float,
        default=3.5,
        help="seconds to wait for the Echo model and details panel to finish loading",
    )
    return parser.parse_args()


def save_png(path: Path, frame) -> None:
    encoded, data = cv2.imencode(".png", frame)
    if not encoded:
        raise RuntimeError(f"failed to encode screenshot: {path}")
    path.write_bytes(data.tobytes())


def xuanling_inventory_build() -> NamedEchoBuild:
    """Adapt the existing Xuanling 43311 fixture to inventory compatibility IDs."""

    scenario = xuanling_scenario()
    compatibility_by_cost = {
        4: "cost_4:crit_damage",
        3: "cost_3:havoc_damage",
        1: "cost_1:attack_percent",
    }
    echoes = tuple(
        NamedEquippedEcho(
            echo.slot,
            EchoIdentity(
                echo.identity.name.split("/", 1)[-1],
                echo.identity.cost,
                compatibility_by_cost[echo.identity.cost],
            ),
            echo.state,
        )
        for echo in scenario.build.echoes
    )
    return NamedEchoBuild(
        scenario.build.profile,
        scenario.build.base_panel,
        echoes,
    )


def serialize_boxes(boxes) -> list[dict]:
    return [
        {
            "text": box.name,
            "confidence": float(box.confidence),
            "x": int(box.x),
            "y": int(box.y),
            "width": int(box.width),
            "height": int(box.height),
        }
        for box in boxes
    ]


def capture(task, output: Path, label: str) -> dict:
    frame = task.executor.method.get_frame()
    if frame is None:
        raise RuntimeError("ok-ww capture returned no frame")
    image_path = output / f"{label}.png"
    save_png(image_path, frame)

    regions = {
        "full": (0, 0, 1, 1),
        "top_controls": (0.10, 0.04, 0.36, 0.17),
        "right_identity": (0.76, 0.12, 0.98, 0.28),
        "right_attributes": (0.76, 0.20, 0.98, 0.50),
        "right_holder": (0.75, 0.82, 0.98, 0.94),
    }
    ocr = {}
    for name, (x, y, to_x, to_y) in regions.items():
        ocr[name] = serialize_boxes(task.ocr(
            x, y, to_x, to_y,
            threshold=0.1,
            frame=frame,
        ))
    return {
        "label": label,
        "image": str(image_path.resolve()),
        "frame_width": int(frame.shape[1]),
        "frame_height": int(frame.shape[0]),
        "ocr": ocr,
    }


def main() -> int:
    args = parse_args()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = args.output or Path("artifacts/echo_page_feasibility") / timestamp
    output.mkdir(parents=True, exist_ok=True)

    config = deepcopy(project_config)
    config["check_mutex"] = False
    config["gui"] = None
    config["custom_tasks"] = False
    config["config_folder"] = str(output / "configs")
    task_class = CheckEchoInventoryTask if args.sequence == "check" else EnhanceEchoTask
    config["onetime_tasks"] = [[task_class.__module__, task_class.__name__]]
    config["trigger_tasks"] = []
    config["windows"] = deepcopy(config["windows"])
    config["windows"]["start_exe"] = False
    config["windows"]["interaction"] = "PostMessage"

    runtime = OK(config)
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
        task = runtime.get_onetime_task(task_class)
        records = [capture(task, output, f"{args.label}_before")]
        scan_result = None
        candidate_inspection = None
        sonata_diagnostic = None
        if args.sequence == "equipped":
            for zone in (
                "equipped_4c",
                "equipped_3c_1",
                "equipped_3c_2",
                "equipped_1c_1",
                "equipped_1c_2",
            ):
                x, y = SAFE_CLICK_ZONES[zone]
                task.click_relative(x, y, after_sleep=args.settle)
                records.append(capture(task, output, f"{args.label}_{zone}"))
            x, y = SAFE_CLICK_ZONES["equipped_4c"]
            task.click_relative(x, y, after_sleep=args.settle)
            records.append(capture(task, output, f"{args.label}_restored_4c"))
        elif args.sequence == "select_role_set":
            x, y = SAFE_CLICK_ZONES["sonata_role_set"]
            task.click_relative(x, y, after_sleep=args.settle)
            records.append(capture(task, output, f"{args.label}_selected"))
            x, y = SAFE_CLICK_ZONES["sonata_filter"]
            task.click_relative(x, y, after_sleep=args.settle)
            records.append(capture(task, output, f"{args.label}_closed"))
        elif args.sequence == "select_sonata":
            if not args.sonata_set:
                raise ValueError("--sonata-set is required for select_sonata")
            selected_set = task.select_echo_sonata_set(
                args.sonata_set,
                settle_time=min(args.settle, 1.5),
            )
            records.append(capture(task, output, f"{args.label}_selected"))
        elif args.sequence == "diagnose_sonata":
            if not args.sonata_set:
                raise ValueError("--sonata-set is required for diagnose_sonata")
            steps = []
            header_text, header_box = task._read_sonata_filter_header()
            steps.append({
                "step": "initial_header",
                "header": header_text,
                "header_box": serialize_boxes([header_box]) if header_box else [],
                "texts": list(getattr(task, "_last_sonata_header_texts", ())),
            })
            if header_box is None:
                raise RuntimeError("cannot find initial Sonata filter entry")
            task._click_capture_box_relative(header_box, after_sleep=args.settle)
            records.append(capture(task, output, f"{args.label}_shell"))
            header_text, header_box = task._read_sonata_filter_header()
            steps.append({
                "step": "shell_header",
                "header": header_text,
                "header_box": serialize_boxes([header_box]) if header_box else [],
                "texts": list(getattr(task, "_last_sonata_header_texts", ())),
            })
            if header_box is None:
                raise RuntimeError("cannot find Sonata dropdown after opening shell")
            task._click_capture_box_relative(header_box, after_sleep=args.settle)
            option_boxes = task._read_sonata_option_boxes()
            steps.append({
                "step": "options",
                "boxes": serialize_boxes(option_boxes),
            })
            records.append(capture(task, output, f"{args.label}_options"))
            target = task._normalize_sonata_set_name(args.sonata_set)
            found = next(
                (box for box in option_boxes
                 if task._normalize_sonata_set_name(box.name) == target),
                None,
            )
            if found is None:
                raise RuntimeError(f"cannot find Sonata option: {args.sonata_set}")
            task._click_capture_box_relative(found, after_sleep=args.settle)
            records.append(capture(task, output, f"{args.label}_clicked_target"))
            verify_header, verify_box = task._read_sonata_filter_header()
            verify_options = task._read_sonata_option_boxes()
            steps.append({
                "step": "after_target",
                "header": verify_header,
                "header_box": serialize_boxes([verify_box]) if verify_box else [],
                "texts": list(getattr(task, "_last_sonata_header_texts", ())),
                "options": serialize_boxes(verify_options),
            })
            sonata_diagnostic = steps
        elif args.sequence == "cost_tabs":
            for zone in ("cost_1", "cost_3", "cost_4"):
                x, y = SAFE_CLICK_ZONES[zone]
                task.click_relative(x, y, after_sleep=args.settle)
                records.append(capture(task, output, f"{args.label}_{zone}"))
        elif args.sequence == "inventory":
            costs = tuple(int(value.strip()) for value in args.costs.split(",") if value.strip())
            scan_result = task.scan_filtered_echo_inventory(
                output_dir=output / "inventory",
                costs=costs,
                completed_only=not args.include_incomplete,
                max_pages_per_cost=args.max_pages,
                settle_time=args.settle,
            )
            records.append(capture(task, output, f"{args.label}_after"))
        elif args.sequence == "inspect_candidate":
            level = task._select_first_fresh_echo(settle_time=args.settle)
            identity = (
                task._read_current_echo_candidate_identity()
                if level == 0 else None
            )
            candidate_inspection = {
                "level": level,
                "eligible_for_enhancement": level == 0,
                "identity": ({
                    "name": identity.name,
                    "cost": identity.cost,
                    "compatibility": identity.compatibility,
                } if identity else None),
                "name_recognition": {
                    "raw": task.info_get("声骸名称OCR原文"),
                    "canonical": task.info_get("声骸名称规范化"),
                    "method": task.info_get("声骸名称匹配方式"),
                    "similarity": task.info_get("声骸名称相似度"),
                    "retries": task.info_get("声骸名称OCR重试"),
                },
            }
            records.append(capture(task, output, f"{args.label}_selected"))
        elif args.sequence == "inspect_model_candidate":
            if args.inspection_result is None:
                raise ValueError("--inspection-result is required")
            reviewed = EchoInventoryCheckResult.read(args.inspection_result)
            best = ModelEnhanceEchoTask._largest_inspected_improvement(reviewed)
            if best is None:
                raise RuntimeError(
                    "第一步检查结果没有可用于确定COST分页的最大提升位置"
                )
            task.set_candidate_cost_order((int(best["cost"]),))
            selected_set = task.select_echo_sonata_set(
                reviewed.sonata_set,
                settle_time=args.settle,
            )
            task.set_echo_enhancement_session(EchoEnhancementSession(
                reviewed.build,
                reviewed.candidate_pool,
                resources=EchoResourceSnapshot.manual(
                    tuners=50,
                    echo_exp=142_600,
                ),
                config=reviewed.policy,
            ))
            level = task._select_first_fresh_echo(settle_time=args.settle)
            identity = task._selected_model_candidate_identity
            candidate_inspection = {
                "sonata_set": selected_set,
                "target_cost": int(best["cost"]),
                "target_slot": str(best["slot"]),
                "level": level,
                "eligible_for_enhancement": level == 0 and identity is not None,
                "enhance_button_found": bool(task.find_echo_enhance()),
                "identity": ({
                    "name": identity.name,
                    "cost": identity.cost,
                    "compatibility": identity.compatibility,
                } if identity else None),
                "name_recognition": {
                    "raw": task.info_get("声骸名称OCR原文"),
                    "canonical": task.info_get("声骸名称规范化"),
                    "method": task.info_get("声骸名称匹配方式"),
                    "similarity": task.info_get("声骸名称相似度"),
                    "retries": task.info_get("声骸名称OCR重试"),
                },
            }
            records.append(capture(task, output, f"{args.label}_selected"))
        elif args.sequence == "prepare_enhancement_sort":
            x, y = SAFE_CLICK_ZONES["sort_filter"]
            task.click_relative(x, y, after_sleep=args.settle)
            records.append(capture(task, output, f"{args.label}_sort_filter"))
            task.next_frame()
            boxes = task.ocr(0, 0, 1, 1, threshold=0.1, frame=task.frame)
            unequipped = next(
                (box for box in boxes if box.name.strip() == "未装备优先"),
                None,
            )
            if unequipped is None:
                raise RuntimeError("排序面板中找不到未装备优先")
            task._click_capture_box_relative(unequipped, after_sleep=args.settle)
            records.append(capture(task, output, f"{args.label}_sort_unequipped"))
            task.next_frame()
            boxes = task.ocr(0, 0, 1, 1, threshold=0.1, frame=task.frame)
            confirm = next(
                (box for box in boxes if box.name.strip() in {"确认", "確認"}),
                None,
            )
            if confirm is None:
                raise RuntimeError("排序面板中找不到确认")
            task._click_capture_box_relative(confirm, after_sleep=args.settle)
            records.append(capture(task, output, f"{args.label}_sort_confirm"))
        elif args.sequence == "check":
            if not args.sonata_set:
                raise ValueError("--sonata-set is required for check")
            task.config["COSTs"] = [
                value.strip() for value in args.costs.split(",") if value.strip()
            ]
            task.config["Max Pages Per COST"] = args.max_pages
            task.config["Settle Seconds"] = args.settle
            task.configure_echo_check(
                sonata_set=args.sonata_set,
                build=xuanling_inventory_build(),
                policy=PolicyConfig(),
                output_dir=str(output / "check"),
            )
            check_result = task.run()
            records.append(capture(task, output, f"{args.label}_after"))
        elif args.sequence == "escape":
            task.send_key("esc", after_sleep=args.settle)
            records.append(capture(task, output, f"{args.label}_after"))
        elif args.sequence == "bag":
            task.send_key("b", after_sleep=args.settle)
            records.append(capture(task, output, f"{args.label}_after"))
        elif args.click_zone:
            x, y = SAFE_CLICK_ZONES[args.click_zone]
            task.click_relative(x, y, after_sleep=args.settle)
            records.append(capture(task, output, f"{args.label}_after"))

        manifest = {
            "created_at": datetime.now().astimezone().isoformat(),
            "click_zone": args.click_zone,
            "sequence": args.sequence,
            "sonata_set": args.sonata_set,
            "selected_sonata_set": (
                selected_set if args.sequence == "select_sonata" else None
            ),
            "click_relative": SAFE_CLICK_ZONES.get(args.click_zone),
            "records": records,
            "inventory_scan": scan_result.to_dict() if scan_result else None,
            "candidate_inspection": candidate_inspection,
            "sonata_diagnostic": sonata_diagnostic,
            "check_result": (
                check_result.to_dict() if args.sequence == "check" else None
            ),
        }
        manifest_path = output / f"{args.label}.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(manifest_path.resolve())
        return 0
    except Exception as error:
        error_path = output / f"{args.label}_error.json"
        error_path.write_text(
            json.dumps({
                "error_type": type(error).__name__,
                "message": str(error),
                "traceback": traceback.format_exc(),
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        raise
    finally:
        runtime.quit()
        runtime.wait_task()
        runtime.device_manager.close()


if __name__ == "__main__":
    raise SystemExit(main())
