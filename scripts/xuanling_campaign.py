"""Snapshot and execute an explicitly requested Xuanling inventory campaign.

Every phase writes its observations before the next phase is launched.
Scoring and enhancement decisions are delegated to the existing Echo model.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from enum import Enum
from datetime import datetime
from itertools import product
import json
import re
from pathlib import Path
import shutil
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import config as project_config
from ok import OK
from scripts.live_echo_enhancement_test import save_webp
from scripts.rebuild_echo_check_from_scan import inventory_item
from src.echo.api import EchoEnhancementSession, EchoResourceSnapshot
from src.echo.optimizer import NamedEchoBuild, NamedEquippedEcho, EchoIdentity
from src.echo.simulation import EchoState, EchoRoll, EchoStat
from src.echo.workflow import EchoInventoryCheckResult, named_echo_build_to_dict, named_echo_build_from_dict
from src.task.EnhanceEchoTask import (
    ECHO_INVENTORY_COST_TABS, ECHO_INVENTORY_GRID_COLUMNS,
    ECHO_INVENTORY_GRID_ROWS, EnhanceEchoTask,
    ECHO_INVENTORY_SORT_DIRECTION,
)


SLOTS = (
    ('4C', 4, 0.235), ('3C-1', 3, 0.400), ('3C-2', 3, 0.500),
    ('1C-1', 1, 0.600), ('1C-2', 1, 0.700),
)
DEFAULT_CHECK = ROOT / 'artifacts/echo_inventory/xuanling_yuluo_final_check_20260826/check/echo_inventory_check.json'


def json_safe(value):
    if is_dataclass(value):
        return json_safe(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_safe(data), ensure_ascii=False, indent=2), encoding='utf-8')


def screen(task, output: Path, label: str):
    task.next_frame()
    frame = task.frame.copy()
    save_webp(output / f'{label}.webp', frame)
    boxes = task.ocr(0, 0, 1, 1, frame=frame, threshold=0.1)
    data = [{'text': box.name, 'x': box.x, 'y': box.y,
             'width': box.width, 'height': box.height} for box in boxes]
    write_json(output / f'{label}.json', data)
    return data


def read_equipped(task, output: Path):
    items = []
    for slot, cost, y in SLOTS:
        task.click_relative(0.064, y, after_sleep=1.5)
        item = task._read_selected_echo_inventory_item(
            cost=cost, page_index=0, cell_index=0,
            screenshot=str(output / f'equipped_{slot}.png'),
        )
        if item.level != 25 or item.state.slot_count != 5:
            raise RuntimeError(f'{slot}: current equipment is not a fully read +25 Echo')
        second = task._read_selected_echo_inventory_item(cost=cost, page_index=0, cell_index=0)
        if item.attribute_key != second.attribute_key:
            raise RuntimeError(f'{slot}: equipment details changed between reads')
        items.append((slot, item))
        write_json(output / 'equipped.json', [dict(item.to_dict(), slot=slot) for slot, item in items])
    return items


def optimize_inventory(build, items):
    """Enumerate legal full builds using the unchanged panel score/ER rules."""
    choices = []
    for equipped in build.echoes:
        pool = [item for item in items if item.level == 25
                and item.identity.cost == equipped.identity.cost
                and item.identity.compatibility == equipped.identity.compatibility]
        if not pool:
            raise RuntimeError(f'No completed candidate for {equipped.slot}')
        choices.append(pool)
    best = None
    best_items = None
    feasible = 0
    evaluated = 0
    for selected in product(*choices):
        if len({item.identity.name for item in selected}) != len(selected):
            continue
        candidate = NamedEchoBuild(build.profile, build.base_panel, tuple(
            NamedEquippedEcho(echo.slot, item.identity, item.state)
            for echo, item in zip(build.echoes, selected, strict=True)
        ))
        evaluated += 1
        if not build.profile.is_feasible(candidate.panel):
            continue
        feasible += 1
        unchanged = sum(a.identity == b.identity and a.state == b.state
                        for a, b in zip(build.echoes, candidate.echoes, strict=True))
        same_names = sum(a.identity.name == b.identity.name
                         for a, b in zip(build.echoes, candidate.echoes, strict=True))
        key = (candidate.score, unchanged, same_names)
        # Interchanging equivalent 1C/3C slots changes floating-point sum
        # order. Treat numerical noise as a tie, then minimize real swaps.
        if (best is None or candidate.score > best[0][0] + 1e-9 or
                (abs(candidate.score - best[0][0]) <= 1e-9 and key[1:] > best[0][1:])):
            best = (key, candidate)
            best_items = selected
    if best is None:
        raise RuntimeError('No complete inventory combination satisfies the existing model constraints')
    return best[1], best_items, evaluated, feasible


def snapshot(task, output: Path, baseline):
    equipped = read_equipped(task, output)
    build = NamedEchoBuild(baseline.build.profile, baseline.build.base_panel, tuple(
        NamedEquippedEcho(slot, item.identity, item.state) for slot, item in equipped
    ))
    write_json(output / 'current_build.json', named_echo_build_to_dict(build))
    task.select_echo_sonata_set(baseline.sonata_set, settle_time=1)
    task.select_echo_inventory_level_descending(settle_time=1)
    scan = task.scan_filtered_echo_inventory(
        output_dir=output / 'inventory', costs=(1, 3, 4), completed_only=True,
        max_pages_per_cost=30, settle_time=1.5,
    )
    if scan.issues or 'max page limit' in scan.stop_reason:
        raise RuntimeError('Inventory scan is incomplete; inspect its issues before equipping')
    plan = build_inventory_plan(build, equipped, scan.items, baseline.sonata_set)
    write_json(output / 'plan.json', plan)
    return plan


def build_inventory_plan(build, equipped, scan_items, sonata_set):
    unique = {item.attribute_key: item for item in scan_items}
    for _, item in equipped:
        unique.setdefault(item.attribute_key, item)
    best, selected, evaluated, feasible = optimize_inventory(build, tuple(unique.values()))
    plan = {
        'created_at': datetime.now().astimezone().isoformat(),
        'sonata_set': sonata_set,
        'current_build': named_echo_build_to_dict(build),
        'optimal_build': named_echo_build_to_dict(best),
        'before_score': build.score, 'optimal_score': best.score,
        'gain': best.score - build.score,
        'evaluated_combinations': evaluated, 'feasible_combinations': feasible,
        'equipped': [dict(item.to_dict(), slot=slot) for slot, item in equipped],
        'inventory': [item.to_dict() for item in unique.values()],
        'selected': [dict(item.to_dict(), slot=echo.slot)
                     for echo, item in zip(best.echoes, selected, strict=True)],
        'per_slot_candidates': {
            echo.slot: [item.to_dict() for item in unique.values()
                        if item.identity.cost == echo.identity.cost
                        and item.identity.compatibility == echo.identity.compatibility]
            for echo in build.echoes
        },
    }
    return plan


def fresh_scan(task, output, baseline):
    if not task.find_echo_enhance():
        task.send_key('esc', after_sleep=1.5)
    if not task.find_echo_enhance():
        raise RuntimeError('Cannot return to Echo inventory')
    task.select_echo_sonata_set(baseline.sonata_set, settle_time=1)
    task.select_echo_inventory_level_descending(settle_time=1)
    task.click_relative(*ECHO_INVENTORY_SORT_DIRECTION, after_sleep=1)
    build = named_echo_build_from_dict(json.loads((output.parent / 'snapshot/current_build.json').read_text(encoding='utf-8')))
    seen = set()
    candidates = {}
    observations = []
    issues = []
    for cost in (1, 3, 4):
        task.click_relative(*ECHO_INVENTORY_COST_TABS[cost], after_sleep=1)
        for _ in range(2):
            task.click_relative(*ECHO_INVENTORY_SORT_DIRECTION, after_sleep=0.5)
        for page in range(30):
            screen(task, output, f'cost{cost}_page{page:02d}')
            page_new = 0
            for cell in range(15):
                row, column = divmod(cell, 3)
                level = task._read_echo_card_level(column, row)
                if level is not None and level != 0:
                    continue
                task.click_relative(ECHO_INVENTORY_GRID_COLUMNS[column], ECHO_INVENTORY_GRID_ROWS[row], after_sleep=0.5)
                if task._read_selected_echo_level() != 0:
                    continue
                try:
                    identity = task._read_current_echo_candidate_identity()
                    key = (identity.name, identity.cost, identity.compatibility)
                    if key not in seen:
                        seen.add(key)
                        page_new += 1
                        observations.append(dict(asdict(identity), page=page, cell=cell))
                        if build.compatible_slots(identity):
                            candidates[key] = identity
                except (ValueError, RuntimeError) as error:
                    issues.append(dict(cost=cost, page=page, cell=cell, error=str(error)))
            write_json(output / 'progress.json', dict(candidates=[asdict(x) for x in candidates.values()], observations=observations, issues=issues))
            if page > 0 and page_new == 0:
                break
            task.scroll_relative(0.25, 0.70, -5)
            task.sleep(1)
        else:
            raise RuntimeError(f'Fresh scan page limit reached at COST {cost}')
    return dict(candidates=[asdict(x) for x in candidates.values()], observations=observations, issues=issues)


def state_from_dict(data):
    return EchoState(tuple(EchoRoll(EchoStat(roll['stat']), roll['tier']) for roll in data['rolls']))


def bulk_enhance(task, output, baseline, *, resume=False):
    root = output.parent
    build = named_echo_build_from_dict(json.loads((root / 'snapshot/current_build.json').read_text(encoding='utf-8')))
    fresh = json.loads((root / 'fresh_scan/result.json').read_text(encoding='utf-8'))
    if fresh.get('error'):
        raise RuntimeError('Fresh inventory scan did not complete')
    # The positive identity allowlist is built only from successful structured
    # observations. Failed reads remain in the source report and cannot enter
    # the pool; live selection independently revalidates each selected item.
    pool = tuple(EchoIdentity(**item) for item in fresh['candidates'])
    session = EchoEnhancementSession(build, pool, resources=EchoResourceSnapshot.manual(
        tuners=10748, echo_exp=1936 * 5000 + 667 * 2000,
        detail='Live cultivation inventory screenshots resources_open and resources_materials'),
        config=baseline.policy)
    task.set_echo_enhancement_session(session)
    task.config['Pause after Success'] = False
    task.config['Max Candidates Per Run'] = 0
    task.config['Max Candidate Search Pages'] = 30
    task.config['Candidate Search Scroll Count'] = 5
    if not resume:
        task.select_echo_sonata_set(baseline.sonata_set, settle_time=1)
    # Fresh scan left level sorting ascending; preserve that direction.
    state = dict(candidate_number=0, identity=None, last_decision=None, stopped_for_resources=False)
    outcomes = []
    previous_state = EchoState()
    if resume:
        progress = json.loads((output / 'progress.json').read_text(encoding='utf-8'))
        for key in state:
            state[key] = progress[key]
        outcomes = progress['outcomes']
        events = [json.loads(line) for line in (output / 'events.jsonl').read_text(encoding='utf-8').splitlines()]
        for entry in events:
            args = entry.get('args', [])
            if entry['kind'] == 'selected':
                previous_state = EchoState()
            elif entry['kind'] == 'record_reveal':
                previous_state = state_from_dict(args[1])
                session.record_reveal(state_from_dict(args[0]), previous_state)
            elif entry['kind'] == 'record_discard':
                session.record_discard(EchoIdentity(**args[0]), state_from_dict(args[1]), args[2])
            elif entry['kind'] == 'record_keep':
                session.record_keep(EchoIdentity(**args[0]), state_from_dict(args[1]), args[2])
        if asdict(session.snapshot()) != progress['snapshot']:
            raise RuntimeError('Journal replay does not match the last persisted resource/build snapshot')

    def event(kind, **data):
        payload = dict(time=datetime.now().astimezone().isoformat(), kind=kind,
                       candidate_number=state['candidate_number'], identity=state['identity'], **data)
        with (output / 'events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(json_safe(payload), ensure_ascii=False) + '\n')
            stream.flush()
        write_json(output / 'progress.json', dict(**state, snapshot=asdict(session.snapshot()),
                   build=named_echo_build_to_dict(session.build), outcomes=outcomes))

    select_original = task._select_first_fresh_echo
    def select(*args, **kwargs):
        level = select_original(*args, **kwargs)
        state['candidate_number'] += 1
        state['identity'] = asdict(task._selected_model_candidate_identity)
        event('selected', level=level)
        return level
    task._select_first_fresh_echo = select

    for method_name in ('advise', 'record_reveal', 'record_discard', 'record_keep'):
        original = getattr(session, method_name)
        def wrapped(*args, _original=original, _name=method_name, **kwargs):
            result = _original(*args, **kwargs)
            if _name == 'advise':
                state['last_decision'] = json_safe(result)
            if _name in ('record_keep', 'record_discard'):
                outcomes.append(dict(number=state['candidate_number'], identity=state['identity'],
                                     outcome=_name, state=json_safe(args[1]), result=json_safe(result)))
            event(_name, args=json_safe(args), result=json_safe(result))
            if _name == 'record_reveal':
                save_webp(output / f"candidate_{state['candidate_number']:04d}_plus{args[1].slot_count * 5}.webp", task.frame.copy())
            return result
        setattr(session, method_name, wrapped)

    screenshot_original = task.screenshot_echo
    task.screenshot_echo = lambda name: screenshot_original(f"campaign_20261009/{state['candidate_number']:04d}/{name}")
    stop_original = task._stop_for_resources
    def stop(cost):
        state['stopped_for_resources'] = True
        event('resources_exhausted', cost=cost)
        return stop_original(cost)
    task._stop_for_resources = stop
    if resume and len(outcomes) < state['candidate_number']:
        identity = EchoIdentity(**state['identity'])
        names = task.ocr(0.09, 0.10, 0.36, 0.19, match=identity.name)
        if not names:
            raise RuntimeError('Current cultivation name does not match interrupted candidate')
        observed = task._read_enhancement_model_state(previous_state)
        second = task._read_enhancement_model_state(previous_state)
        if observed != second:
            raise RuntimeError('Recovery reads disagree')
        event('resume_verified', previous=previous_state, observed=observed)
        session.record_reveal(previous_state, observed)
        task.config['Max Candidates Per Run'] = 1
        task.run(resume_candidate=(identity, observed))
        task.config['Max Candidates Per Run'] = 0
    ranked = sorted(session._campaign.fresh_options(), key=lambda pair: pair[1].efficiency, reverse=True)
    cost_order = tuple(dict.fromkeys(identity.cost for identity, _ in ranked))
    event('start', cost_order=cost_order, resources=asdict(session.resources))
    for cost in cost_order:
        task.set_candidate_cost_order((cost,))
        task.run()
        event('cost_finished', cost=cost, task_info=json_safe(task.info))
        if state['stopped_for_resources']:
            break
    return dict(**state, snapshot=asdict(session.snapshot()), build=named_echo_build_to_dict(session.build), outcomes=outcomes)


def final_scan(task, output, baseline):
    if not task.find_echo_enhance():
        task.send_key('esc', after_sleep=1.5)
    equipped = read_equipped(task, output)
    build = NamedEchoBuild(baseline.build.profile, baseline.build.base_panel, tuple(
        NamedEquippedEcho(slot, item.identity, item.state) for slot, item in equipped))
    task.select_echo_sonata_set(baseline.sonata_set, settle_time=1)
    task.select_echo_inventory_level_descending(settle_time=1)
    items = {}
    for cost in (1, 3, 4):
        task.click_relative(*ECHO_INVENTORY_COST_TABS[cost], after_sleep=1)
        for _ in range(2):
            task.click_relative(*ECHO_INVENTORY_SORT_DIRECTION, after_sleep=0.5)
        done = False
        for page in range(30):
            new_count = 0
            for cell in range(15):
                row, column = divmod(cell, 3)
                task.click_relative(ECHO_INVENTORY_GRID_COLUMNS[column], ECHO_INVENTORY_GRID_ROWS[row], after_sleep=1)
                for attempt in range(3):
                    try:
                        item = task._read_selected_echo_inventory_item(cost=cost, page_index=page, cell_index=cell,
                            screenshot=str(output / f'cost{cost}_page{page}_cell{cell}_try{attempt}.png'))
                        break
                    except (ValueError, RuntimeError):
                        if attempt == 2:
                            raise
                        task.sleep(0.7)
                if item.level != 25:
                    done = True
                    break
                if item.attribute_key not in items:
                    items[item.attribute_key] = item
                    new_count += 1
            write_json(output / 'inventory.json', [item.to_dict() for item in items.values()])
            if done or (page > 0 and new_count == 0):
                break
            task.scroll_relative(0.25, 0.70, -5)
            task.sleep(1)
        else:
            raise RuntimeError(f'Final scan page limit at COST {cost}')
    plan = build_inventory_plan(build, equipped, tuple(items.values()), baseline.sonata_set)
    expected_path = output.parent / 'pre_equip_final_plan.json'
    if expected_path.exists():
        expected = json.loads(expected_path.read_text(encoding='utf-8'))
        expected_keys = {inventory_item(item).attribute_key for item in expected['inventory']}
        if not expected_keys.issubset(items):
            raise RuntimeError('Final scan missed known completed Echoes; verify descending sort before retrying')
    write_json(output / 'plan.json', plan)
    return plan


def equip_plan(task, output, baseline):
    plan = json.loads((output.parent / 'final_scan/plan.json').read_text(encoding='utf-8'))
    changes = []
    for slot, cost, y in SLOTS:
        target = inventory_item(next(item for item in plan['selected'] if item['slot'] == slot))
        task.click_relative(0.064, y, after_sleep=1)
        current = task._read_selected_echo_inventory_item(cost=cost, page_index=0, cell_index=0)
        if current.attribute_key == target.attribute_key:
            continue
        task.click_relative(*ECHO_INVENTORY_COST_TABS[cost], after_sleep=1)
        for _ in range(2):
            task.click_relative(*ECHO_INVENTORY_SORT_DIRECTION, after_sleep=0.5)
        found = False
        for page in range(30):
            for cell in range(15):
                row, column = divmod(cell, 3)
                if task._read_echo_card_level(column, row) not in (None, 25):
                    continue
                task.click_relative(ECHO_INVENTORY_GRID_COLUMNS[column], ECHO_INVENTORY_GRID_ROWS[row], after_sleep=0.7)
                item = task._read_selected_echo_inventory_item(cost=cost, page_index=page, cell_index=cell)
                if item.attribute_key != target.attribute_key:
                    continue
                found = True
                break
            if found:
                break
            task.scroll_relative(0.25, 0.70, -5)
            task.sleep(1)
        if not found:
            raise RuntimeError(f'Could not locate exact target Echo for {slot}')
        screen(task, output, f'{slot}_before_replace')
        y_offset = task._echo_inventory_capture_y_offset(task.frame)
        buttons = task.ocr(0.78, 0.88 + y_offset, 0.89,
                           min(1.0, 0.96 + y_offset), match=re.compile('替换|装配'))
        if not buttons:
            raise RuntimeError(f'{slot}: exact target selected but replacement button missing')
        task._click_capture_box_relative(buttons[0], after_sleep=1.5)
        screen(task, output, f'{slot}_after_replace')
        task.click_relative(0.064, y, after_sleep=1)
        verify = task._read_selected_echo_inventory_item(cost=cost, page_index=0, cell_index=0)
        if verify.attribute_key != target.attribute_key:
            raise RuntimeError(f'{slot}: equipped stats do not match selected target')
        changes.append(dict(slot=slot, before=current.to_dict(), after=verify.to_dict()))
        write_json(output / 'changes.json', changes)
    verified = read_equipped(task, output)
    actual = NamedEchoBuild(baseline.build.profile, baseline.build.base_panel, tuple(
        NamedEquippedEcho(slot, item.identity, item.state) for slot, item in verified))
    if abs(actual.score - plan['optimal_score']) > 1e-8 or not actual.profile.is_feasible(actual.panel):
        raise RuntimeError('Final equipped build does not equal planned feasible optimum')
    return dict(changes=changes, verified_build=named_echo_build_to_dict(actual), score=actual.score)


def audit_resources(task, output):
    task.click_relative(*ECHO_INVENTORY_COST_TABS[1], after_sleep=1)
    task.select_echo_inventory_level_descending(settle_time=1)
    task.click_relative(*ECHO_INVENTORY_SORT_DIRECTION, after_sleep=1)
    selected = None
    for cell in range(15):
        row, column = divmod(cell, 3)
        task.click_relative(ECHO_INVENTORY_GRID_COLUMNS[column], ECHO_INVENTORY_GRID_ROWS[row], after_sleep=0.6)
        if task._read_selected_echo_level() == 0:
            selected = task._read_selected_echo_inventory_item(cost=1, page_index=0, cell_index=cell)
            break
    if selected is None:
        raise RuntimeError('No remaining fresh Echo available to open read-only material inventory')
    buttons = task.find_echo_enhance()
    if not buttons:
        raise RuntimeError('Cannot open selected Echo cultivation for material audit')
    task._click_capture_box_relative(buttons[0], after_sleep=1)
    cultivation = screen(task, output, 'cultivation')
    task.click_relative(0.135, 0.742, after_sleep=1)
    materials = screen(task, output, 'materials')
    task.send_key('esc', after_sleep=0.8)
    task.send_key('esc', after_sleep=1)
    task.click_relative(0.064, 0.600, after_sleep=1)
    return dict(selected_for_reading=selected.to_dict(), cultivation=cultivation, materials=materials)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['snapshot', 'observe', 'read-cell', 'open-cultivation', 'open-selected', 'click', 'escape', 'fresh-scan', 'bulk', 'resume', 'final-scan', 'equip', 'audit-resources', 'finalize'])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--baseline', type=Path, default=DEFAULT_CHECK)
    parser.add_argument('--cost', type=int, choices=[1, 3, 4])
    parser.add_argument('--cell', type=int, default=0)
    parser.add_argument('--x', type=float)
    parser.add_argument('--y', type=float)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.phase == 'resume':
        archive = output / ('interruption_' + datetime.now().strftime('%H%M%S'))
        archive.mkdir()
        for name in ('before.webp', 'before.json', 'after.webp', 'after.json', 'result.json', 'progress.json'):
            if (output / name).exists():
                shutil.copy2(output / name, archive / name)
        # Keep the prior error as evidence, while making result.json a marker
        # of this resumed process finishing again.
        (output / 'result.json').rename(archive / 'original_result.json')
    config = deepcopy(project_config)
    config.update(check_mutex=False, gui=None, custom_tasks=False,
                  config_folder=str(output / 'configs'),
                  onetime_tasks=[[EnhanceEchoTask.__module__, EnhanceEchoTask.__name__]],
                  trigger_tasks=[])
    config['windows'] = deepcopy(config['windows'])
    config['windows'].update(start_exe=False, interaction='PostMessage')
    runtime = OK(config)
    task = None
    result = {}
    code = 0
    try:
        runtime.start_runtime()
        for _ in range(30):
            if runtime.task_executor.method is not None and runtime.task_executor.method.connected():
                break
            runtime.device_manager.refresh()
            time.sleep(0.5)
        else:
            raise RuntimeError('Cannot connect to game window')
        runtime.task_executor.paused = False
        task = runtime.get_onetime_task(EnhanceEchoTask)
        screen(task, output, 'before')
        if args.phase == 'finalize':
            baseline = EchoInventoryCheckResult.read(args.baseline)
            for name, operation in (('final_scan', final_scan), ('equip', equip_plan),
                                    ('resource_final', audit_resources)):
                phase_output = output.parent / name
                phase_output.mkdir(parents=True, exist_ok=True)
                phase_result = (operation(task, phase_output) if name == 'resource_final'
                                else operation(task, phase_output, baseline))
                write_json(phase_output / 'result.json', phase_result)
                result[name] = phase_result
        elif args.phase == 'snapshot':
            result = snapshot(task, output, EchoInventoryCheckResult.read(args.baseline))
        elif args.phase == 'fresh-scan':
            result = fresh_scan(task, output, EchoInventoryCheckResult.read(args.baseline))
        elif args.phase == 'bulk':
            result = bulk_enhance(task, output, EchoInventoryCheckResult.read(args.baseline))
        elif args.phase == 'resume':
            result = bulk_enhance(task, output, EchoInventoryCheckResult.read(args.baseline), resume=True)
        elif args.phase == 'final-scan':
            result = final_scan(task, output, EchoInventoryCheckResult.read(args.baseline))
        elif args.phase == 'equip':
            result = equip_plan(task, output, EchoInventoryCheckResult.read(args.baseline))
        elif args.phase == 'audit-resources':
            result = audit_resources(task, output)
        elif args.phase == 'read-cell':
            task.click_relative(*ECHO_INVENTORY_COST_TABS[args.cost], after_sleep=1.5)
            row, column = divmod(args.cell, 3)
            task.click_relative(ECHO_INVENTORY_GRID_COLUMNS[column],
                                ECHO_INVENTORY_GRID_ROWS[row], after_sleep=2)
            item = task._read_selected_echo_inventory_item(
                cost=args.cost, page_index=0, cell_index=args.cell,
                screenshot=str(output / 'item.png'),
            )
            second = task._read_selected_echo_inventory_item(
                cost=args.cost, page_index=0, cell_index=args.cell,
            )
            if item.attribute_key != second.attribute_key:
                raise RuntimeError('Cell details differ between repeated reads')
            result = item.to_dict()
        elif args.phase == 'open-cultivation':
            baseline = EchoInventoryCheckResult.read(args.baseline)
            # Only select/read a fresh candidate here. No decision or upgrade
            # is executed; actual resource quantities will be read next.
            task.set_echo_enhancement_session(EchoEnhancementSession(
                baseline.build, baseline.candidate_pool,
                resources=EchoResourceSnapshot.manual(tuners=0, echo_exp=0),
                config=baseline.policy,
            ))
            task.set_candidate_cost_order((1,))
            task._select_first_fresh_echo(settle_time=0.5)
            result['candidate'] = asdict(task._selected_model_candidate_identity)
            buttons = task.find_echo_enhance()
            if not buttons:
                raise RuntimeError('No cultivation button on selected fresh candidate')
            task._click_capture_box_relative(buttons[0], after_sleep=1.5)
        elif args.phase == 'open-selected':
            buttons = task.find_echo_enhance()
            if not buttons:
                raise RuntimeError('No cultivation button on selected Echo')
            task._click_capture_box_relative(buttons[0], after_sleep=1.5)
        elif args.phase == 'click':
            if args.x is None or args.y is None:
                raise ValueError('click requires observed client-relative x and y')
            task.click_relative(args.x, args.y, after_sleep=1.5)
        elif args.phase == 'escape':
            task.send_key('esc', after_sleep=1.5)
    except Exception as error:
        result['error'] = {'message': str(error), 'traceback': traceback.format_exc()}
        code = 1
    finally:
        if task is not None:
            screen(task, output, 'after')
        write_json(output / 'result.json', result)
        runtime.quit()
        runtime.wait_task()
        runtime.device_manager.close()
    return code


if __name__ == '__main__':
    raise SystemExit(main())
