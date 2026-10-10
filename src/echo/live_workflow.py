"""Reusable live Echo inspection helpers; no historical campaign inputs.

Importing this module does not create an OK runtime or connect to the game.
The caller owns the runtime and must ensure exclusive control of the game.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, is_dataclass
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
import time

from src.echo.optimizer import EchoIdentity, NamedEchoBuild, NamedEquippedEcho


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EQUIPMENT_SLOTS = (
    ('4C', 4, .235), ('3C-1', 3, .400), ('3C-2', 3, .500),
    ('1C-1', 1, .600), ('1C-2', 1, .700),
)


def json_safe(value):
    if is_dataclass(value):
        return json_safe(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(json_safe(data), ensure_ascii=False,
                                    indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def capture_snapshot(task, output, label, *, include_ocr=False):
    import cv2
    task.next_frame()
    frame = task.frame.copy()
    path = Path(output) / f'{label}.webp'
    success, encoded = cv2.imencode('.webp', frame, [cv2.IMWRITE_WEBP_QUALITY, 62])
    if not success:
        raise RuntimeError(f'Cannot encode screenshot: {path}')
    path.write_bytes(encoded.tobytes())
    result = {'screenshot': str(path)}
    if include_ocr:
        result['ocr'] = [dict(text=box.name, x=box.x, y=box.y,
                              width=box.width, height=box.height)
                         for box in task.ocr(0, 0, 1, 1, frame=frame, threshold=.1)]
        write_json(path.with_suffix('.json'), result['ocr'])
    return result


def read_equipped(task, output, *, settle_time=1.5, attempts=3):
    """Read all five slots twice; retries never spend materials or equip."""
    items = []
    for slot, cost, y in EQUIPMENT_SLOTS:
        for attempt in range(attempts):
            try:
                task.click_relative(.064, y, after_sleep=settle_time)
                first = task._read_selected_echo_inventory_item(
                    cost=cost, page_index=0, cell_index=0,
                    screenshot=str(Path(output) / f'equipped_{slot}.png'))
                task.sleep(.3)
                second = task._read_selected_echo_inventory_item(
                    cost=cost, page_index=0, cell_index=0)
                if (first.level != 25 or second.level != 25 or
                        first.state.slot_count != 5 or second.state.slot_count != 5 or
                        first.identity.cost != cost or first.attribute_key != second.attribute_key):
                    raise RuntimeError(f'{slot}: inconsistent or incomplete +25 equipment')
                break
            except (RuntimeError, ValueError):
                if attempt == attempts - 1:
                    raise
                task.sleep(settle_time)
        items.append((slot, first))
        write_json(Path(output) / 'equipped.json',
                   [dict(item.to_dict(), slot=key) for key, item in items])
    return items


def observed_build(model, equipped):
    expected = {echo.slot: echo.identity for echo in model.echoes}
    if set(expected) != {slot for slot, _ in equipped}:
        raise ValueError('Observed equipment slots do not match the model')
    for slot, item in equipped:
        identity = expected[slot]
        if (identity.cost, identity.compatibility) != (item.identity.cost, item.identity.compatibility):
            raise ValueError(f'{slot}: observed COST/main stat does not match the selected template')
    return NamedEchoBuild(model.profile, model.base_panel, tuple(
        NamedEquippedEcho(slot, item.identity, item.state) for slot, item in equipped))


def validate_inspection(check):
    scan = check.inspection.get('scan')
    if not isinstance(scan, dict) or not isinstance(scan.get('items'), list):
        raise ValueError('Inspection must contain a current structured inventory scan')
    if scan.get('issues') or 'max page limit' in str(scan.get('stop_reason', '')).lower():
        raise ValueError('Inventory scan is incomplete; resolve its evidence before live enhancement')
    if len(check.build.echoes) != 5 or any(echo.state.slot_count != 5 for echo in check.build.echoes):
        raise ValueError('Inspection must contain five fully read equipped Echoes')
    if not check.candidate_pool:
        raise ValueError('Inspection has no eligible candidate types')
    check.completed_inventory()  # Validate all completed inventory records now.


def check_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stock_document(check_path, records):
    return dict(kind='ok-ww.echo-verified-missing-stock', schema_version=1,
                inspection_sha256=check_digest(check_path), records=list(records))


def load_missing_stock(path, check):
    """Bind explicit zero-stock evidence to the exact current check bytes."""
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if (data.get('kind') != 'ok-ww.echo-verified-missing-stock' or
            data.get('schema_version') != 1 or
            data.get('inspection_sha256') != check_digest(check.source_path)):
        raise ValueError('Verified stock must be generated for this exact inspection file')
    records = data.get('records')
    if not isinstance(records, list):
        raise ValueError('Verified stock records must be an array')
    result = []
    seen = set()
    for row in records:
        identity = EchoIdentity(**row['identity'])
        evidence = str(row.get('evidence') or '').strip()
        if (row.get('count') != 0 or isinstance(row.get('count'), bool) or
                identity not in check.candidate_pool or identity in seen or not evidence):
            raise ValueError('Each stock record must be a unique current candidate with zero-stock evidence')
        seen.add(identity)
        result.append((identity, evidence))
    return tuple(result)


def runtime_config(project_config, output):
    config = deepcopy(project_config)
    config.update(check_mutex=True, gui=None, custom_tasks=False,
                  config_folder=str(Path(output) / 'configs'),
                  screenshots_folder=str(Path(output) / 'screenshots'),
                  log_file=str(Path(output) / 'runtime.log'),
                  error_log_file=str(Path(output) / 'error.log'),
                  trigger_tasks=[], onetime_tasks=[
                      ['src.task.CheckEchoInventoryTask', 'CheckEchoInventoryTask'],
                      ['src.task.ModelEnhanceEchoTask', 'ModelEnhanceEchoTask']])
    config['windows'].update(start_exe=False, interaction='PostMessage')
    return config


def create_runtime(output):
    from config import config
    from ok import OK
    return OK(runtime_config(config, output))


def connect_runtime(runtime, *, timeout=15):
    runtime.start_runtime()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        method = runtime.task_executor.method
        if method is not None and method.connected():
            runtime.task_executor.paused = False
            return
        runtime.device_manager.refresh()
        time.sleep(.5)
    raise RuntimeError('Cannot connect to the running game window')


def close_runtime(runtime):
    """Try every cleanup step; report errors rather than hiding the result."""
    errors = []
    for name, action in (('quit', runtime.quit), ('wait_task', runtime.wait_task),
                         ('device_manager.close', runtime.device_manager.close)):
        try:
            action()
        except Exception as error:
            errors.append(dict(step=name, type=type(error).__name__, message=str(error)))
    return errors


def audit_stock(task, check, output, *, cost, max_pages=30, settle_time=1.5, scroll_count=5):
    """Independent two-order audit using complete ordered card fingerprints.

    Page limits and unreadable cards remain errors, never evidence of zero.
    No enhancement, lock, discard, equip or recycle actions are performed.
    """
    from src.task.EnhanceEchoTask import (
        ECHO_INVENTORY_COST_TABS, ECHO_INVENTORY_GRID_COLUMNS,
        ECHO_INVENTORY_GRID_ROWS, ECHO_INVENTORY_SORT_DIRECTION)
    task.select_echo_sonata_set(check.sonata_set, settle_time=settle_time)
    task.click_relative(*ECHO_INVENTORY_COST_TABS[cost], after_sleep=settle_time)
    task.select_echo_inventory_level_descending(settle_time=settle_time)
    directions = []
    fresh = {}
    observed = {}
    for direction in range(2):
        for _ in range(2 if direction == 0 else 1):
            task.click_relative(*ECHO_INVENTORY_SORT_DIRECTION, after_sleep=settle_time)
        previous = None
        pages = []
        for page in range(max_pages):
            items = []
            for row, y in enumerate(ECHO_INVENTORY_GRID_ROWS):
                for column, x in enumerate(ECHO_INVENTORY_GRID_COLUMNS):
                    task.click_relative(x, y, after_sleep=settle_time)
                    first = task._read_selected_echo_inventory_item(
                        cost=cost, page_index=page,
                        cell_index=row * len(ECHO_INVENTORY_GRID_COLUMNS) + column)
                    task.sleep(.3)
                    second = task._read_selected_echo_inventory_item(
                        cost=cost, page_index=page,
                        cell_index=row * len(ECHO_INVENTORY_GRID_COLUMNS) + column)
                    if (first.level, first.attribute_key) != (second.level, second.attribute_key):
                        raise RuntimeError('Inventory card fingerprint changed between reads')
                    items.append(first)
                    observed[first.attribute_key] = first.to_dict()
                    if first.level == 0:
                        fresh[first.identity] = first.to_dict()
            keys = [(item.level, item.attribute_key) for item in items]
            pages.append([item.to_dict() for item in items])
            write_json(Path(output) / f'direction_{direction}_pages.json', pages)
            capture_snapshot(task, output, f'direction_{direction}_page_{page}')
            if keys == previous:
                directions.append(dict(direction=direction, pages=len(pages),
                                       reached_unchanged_fingerprint_end=True))
                break
            previous = keys
            task.scroll_relative(.25, .70, -scroll_count)
            task.sleep(settle_time)
        else:
            raise RuntimeError('Fingerprint audit reached page limit; no shortage conclusion')
    records = [dict(identity=asdict(identity), count=0,
                    evidence=f'{Path(output).resolve()}: both orders reached unchanged full-card fingerprints')
               for identity in check.candidate_pool if identity.cost == cost and identity not in fresh]
    return dict(read_only=True, double_read_confirmed=True,
                inspection_sha256=check_digest(check.source_path),
                directions=directions, items=list(observed.values()),
                fresh_items=list(fresh.values()), records=records)
