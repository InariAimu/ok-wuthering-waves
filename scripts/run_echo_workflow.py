"""Run a fresh, self-contained Echo check or adaptive enhancement process.

Examples and game-page prerequisites: docs/echo-enhancement-agent-guide.md.
No previous artifacts, GUI configs, or running OK-WW instance are required.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import sys
import traceback

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

DEFAULT_ECHO_CHARACTER_TEMPLATE = REPOSITORY_ROOT / 'echo_character_templates.json'


def create_runtime(output):
    from src.echo.live_workflow import create_runtime as factory
    return factory(output)


def connect_runtime(runtime):
    from src.echo.live_workflow import connect_runtime as connect
    return connect(runtime)


def close_runtime(runtime):
    from src.echo.live_workflow import close_runtime as close
    return close(runtime)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='phase', required=True)
    for phase in ('observe', 'equipment', 'check', 'live', 'stock'):
        command = subparsers.add_parser(phase)
        command.add_argument('--output', type=Path, help='New directory for this invocation')
        command.add_argument('--settle', type=float, default=1.5)
        if phase in ('equipment', 'check'):
            command.add_argument('--character', required=True)
            command.add_argument('--templates', type=Path, default=DEFAULT_ECHO_CHARACTER_TEMPLATE)
        if phase in ('check', 'live', 'stock'):
            command.add_argument('--max-pages', type=int, default=30)
            command.add_argument('--scroll-count', type=int, default=5)
        if phase in ('live', 'stock'):
            command.add_argument('--inspection', type=Path, required=True)
        if phase == 'stock':
            command.add_argument('--cost', type=int, choices=(1, 3, 4), required=True)
        if phase == 'live':
            command.add_argument('--tuners', type=float, required=True)
            command.add_argument('--echo-exp', type=float, required=True,
                                 help='Verified model EXP budget, not tube count')
            command.add_argument('--max-candidates', type=int, default=0,
                                 help='0: continue until policy/resource/stock stop')
            command.add_argument('--verified-stock', type=Path,
                                 help='Explicit current-check zero-stock evidence from stock/audit')
            command.add_argument('--switch-ratio', type=float, default=1.25)
            command.add_argument('--missing-ratio', type=float, default=.5)
            command.add_argument('--fresh-samples', type=int, default=20000)
            command.add_argument('--maximum-samples', type=int, default=80000)
    return parser.parse_args(argv)


def prepare_inputs(args):
    from src.echo.character_templates import load_echo_character_template
    from src.echo.live_workflow import load_missing_stock, validate_inspection
    from src.echo.workflow import EchoInventoryCheckResult
    if not math.isfinite(args.settle) or args.settle < 0:
        raise ValueError('--settle must be finite and non-negative')
    if args.phase in ('check', 'live', 'stock'):
        if args.max_pages < 1 or args.scroll_count < 1:
            raise ValueError('Page/scroll limits must be positive')
    inputs = {}
    if args.phase in ('equipment', 'check'):
        inputs['template'] = load_echo_character_template(args.templates, args.character)
    if args.phase in ('live', 'stock'):
        check = EchoInventoryCheckResult.read(args.inspection)
        validate_inspection(check)
        inputs['check'] = check
    if args.phase == 'live':
        from src.echo.adaptive import AdaptivePolicy
        if (not all(math.isfinite(value) and value > 0 for value in (args.tuners, args.echo_exp))
                or args.max_candidates < 0):
            raise ValueError('Live mode requires positive verified budgets and a non-negative candidate limit')
        AdaptivePolicy(switch_efficiency_ratio=args.switch_ratio,
                       missing_candidate_stop_ratio=args.missing_ratio,
                       fresh_samples=args.fresh_samples, maximum_samples=args.maximum_samples)
        inputs['missing_stock'] = (load_missing_stock(args.verified_stock, inputs['check'])
                                   if args.verified_stock else ())
    return inputs


def run_operation(runtime, args, output, inputs, report):
    from src.echo.live_workflow import (
        audit_stock, capture_snapshot, observed_build, read_equipped,
        stock_document, validate_inspection, write_json)
    from src.echo.workflow import named_echo_build_to_dict
    from src.task.CheckEchoInventoryTask import CheckEchoInventoryTask
    from src.task.ModelEnhanceEchoTask import ModelEnhanceEchoTask
    task = runtime.get_onetime_task(ModelEnhanceEchoTask if args.phase == 'live' else CheckEchoInventoryTask)
    report['initial_state'] = capture_snapshot(task, output, 'before')
    if args.phase == 'observe':
        report['observation'] = capture_snapshot(task, output, 'observation', include_ocr=True)
    elif args.phase in ('equipment', 'check'):
        template = inputs['template']
        equipped = read_equipped(task, output, settle_time=args.settle)
        build = observed_build(template.build, equipped)
        report.update(character=args.character, sonata_set=template.sonata_set,
                      verified_build=named_echo_build_to_dict(build),
                      double_read_confirmed=True, equipment_applied_in_game=False)
        if args.phase == 'check':
            task.configure_echo_check(sonata_set=template.sonata_set, build=build,
                                      candidate_pool=template.candidate_pool, policy=template.policy,
                                      output_dir=str(output / 'check'))
            task.config['COSTs'] = ['1', '3', '4']
            task.config['Max Pages Per COST'] = args.max_pages
            task.config['Scroll Count'] = args.scroll_count
            task.config['Settle Seconds'] = args.settle
            check = task.run()
            validate_inspection(check)
            report['inspection'] = str(output / 'check' / 'echo_inventory_check.json')
    elif args.phase == 'stock':
        report['stock_audit'] = audit_stock(task, inputs['check'], output, cost=args.cost,
                                           max_pages=args.max_pages, settle_time=args.settle,
                                           scroll_count=args.scroll_count)
        stock_path = output / 'verified_missing_stock.json'
        write_json(stock_path, stock_document(args.inspection, report['stock_audit']['records']))
        report['verified_stock'] = str(stock_path)
    elif args.phase == 'live':
        task.load_inspection_result(args.inspection)
        task.configure_live_observations(missing_stock=inputs['missing_stock'], search_settle_time=args.settle)
        for key, value in {
            'Inspection Result File': str(args.inspection), 'Simulation Only': False,
            'Resource Source': 'Manual', 'Manual Tuners': args.tuners,
            'Manual Premium Echo EXP Tubes': 0, 'Manual Echo EXP': args.echo_exp,
            'Max Candidates Per Run': args.max_candidates, 'Pause after Success': False,
            'Max Candidate Search Pages': args.max_pages,
            'Candidate Search Scroll Count': args.scroll_count,
            'Candidate Switch Ratio': args.switch_ratio, 'Missing Candidate Stop Ratio': args.missing_ratio,
            'Fresh Candidate Samples': args.fresh_samples, 'Maximum Decision Samples': args.maximum_samples,
            'Decision Log Directory': str(output / 'decisions'),
        }.items():
            task.config[key] = value
        report.update(inspection=str(args.inspection),
                      budget=dict(tuners=args.tuners, echo_exp=args.echo_exp),
                      equipment_applied_in_game=False, recycling_executed=False)
        try:
            task.run()
        finally:
            report['task_info'] = dict(task.info)
            session = getattr(task, '_echo_enhancement_session', None)
            if session is not None:
                report['snapshot'] = session.audit_snapshot()
                if session.journal:
                    report['decision_directory'] = str(session.journal.directory)
        if report.get('snapshot', {}).get('unreconciled_upgrade'):
            raise RuntimeError('Enhancement returned with an unreconciled upgrade; inspect the journal before retrying')
    report['final_state'] = capture_snapshot(task, output, 'after')


def main(argv=None):
    args = parse_args(argv)
    from src.echo.live_workflow import write_json
    # Resolve user inputs before changing cwd. Assets and project defaults are
    # then resolved from this checkout, never from an old machine's directory.
    for key in ('inspection', 'templates', 'verified_stock'):
        path = getattr(args, key, None)
        if path is not None:
            setattr(args, key, path.expanduser().resolve())
    output = (args.output or REPOSITORY_ROOT / 'artifacts' / 'echo_inventory' / 'workflow_runs' /
              datetime.now().strftime('%Y%m%d_%H%M%S_%f')).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    report_path = output / 'result.json'
    report = dict(kind='ok-ww.echo-live-workflow', schema_version=1,
                  phase=args.phase, status='running', error=None, cleanup_errors=[],
                  started_at=datetime.now(timezone.utc).isoformat(),
                  arguments=vars(args), output=str(output))
    write_json(report_path, report)
    runtime = None
    previous_cwd = Path.cwd()
    try:
        inputs = prepare_inputs(args)  # Reject stale evidence/bad inputs before touching the game.
        os.chdir(REPOSITORY_ROOT)
        runtime = create_runtime(output)
        connect_runtime(runtime)
        run_operation(runtime, args, output, inputs, report)
        report['status'] = 'completed'
    except (Exception, KeyboardInterrupt) as error:
        report['status'] = 'error'
        report['error'] = dict(type=type(error).__name__, message=str(error), traceback=traceback.format_exc())
    finally:
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        # Persist failure before cleanup: a shutdown problem must not hide a
        # spent-materials error from an agent waiting on this process.
        write_json(report_path, report)
        try:
            if runtime is not None:
                report['cleanup_errors'] = close_runtime(runtime)
                if report['cleanup_errors']:
                    report['status'] = 'error'
            write_json(report_path, report)
        finally:
            os.chdir(previous_cwd)
    print(report_path)
    return 0 if report['status'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
