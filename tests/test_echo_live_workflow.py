import contextlib
from dataclasses import replace
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import run_echo_workflow
from scripts.reconcile_echo_inventory import reconcile
from src.echo.adaptive import AdaptiveEnhancementSession, AdaptivePolicy
from src.echo.api import EchoResourceSnapshot
from src.echo.character_templates import DEFAULT_ECHO_CHARACTER_TEMPLATE, load_echo_character_template
from src.echo.decision_log import DecisionJournal
from src.echo.inventory import EchoInventoryItem, ObservedEchoStat, inventory_item_from_dict
from src.echo.live_workflow import (
    audit_stock, check_digest, load_missing_stock, observed_build, read_equipped,
    runtime_config, stock_document, validate_inspection, write_json,
)
from src.echo.run_audit import audit_runs, write_audit
from src.echo.simulation import EchoRoll, EchoState, EchoStat
from src.echo.workflow import EchoInventoryCheckResult
from src.task.ModelEnhanceEchoTask import ModelEnhanceEchoTask


def make_check(directory):
    context = load_echo_character_template(DEFAULT_ECHO_CHARACTER_TEMPLATE, '秧秧·玄翎')
    check = EchoInventoryCheckResult(context.sonata_set, context.build, context.candidate_pool,
        context.policy, {'scan': dict(items=[], issues=[], pages_scanned=3,
                                    duplicate_fingerprints=0, stop_reason='test full scan')}, '2026-10-10')
    path = check.write(Path(directory) / 'check.json')
    return EchoInventoryCheckResult.read(path)


def item_for(echo, *, level=25):
    return EchoInventoryItem(echo.identity, level,
        (ObservedEchoStat(EchoStat.ATTACK_PERCENT, 33, '攻击', '33%'),),
        echo.state if level == 25 else EchoState(), 0, 0)


class TestPortableLiveWorkflow(unittest.TestCase):
    def test_both_character_models_load_without_saved_gui_configs(self):
        for name in ('秧秧·玄翎', '心'):
            template = load_echo_character_template(DEFAULT_ECHO_CHARACTER_TEMPLATE, name)
            self.assertEqual(len(template.build.echoes), 5)
            self.assertTrue(all(item.state.slot_count == 5 for item in template.build.echoes))

    def test_runtime_has_isolated_paths_and_no_background_tasks(self):
        original = dict(windows={'exe': 'game.exe', 'start_exe': True},
                        trigger_tasks=['combat'], onetime_tasks=['old'])
        config = runtime_config(original, Path('fresh-output'))
        self.assertTrue(config['check_mutex'])
        self.assertEqual(config['trigger_tasks'], [])
        self.assertEqual(config['config_folder'], str(Path('fresh-output/configs')))
        self.assertFalse(config['windows']['start_exe'])
        self.assertTrue(original['windows']['start_exe'])

    def test_equipment_second_read_must_also_be_full_level(self):
        with tempfile.TemporaryDirectory() as directory:
            check = make_check(directory)
            item = item_for(check.build.echoes[0])
            task = SimpleNamespace(click_relative=Mock(), sleep=Mock(),
                _read_selected_echo_inventory_item=Mock(side_effect=[item, replace(item, level=20)]))
            with self.assertRaisesRegex(RuntimeError, 'inconsistent'):
                read_equipped(task, directory, attempts=1)

    def test_actual_main_stats_must_match_the_selected_model(self):
        with tempfile.TemporaryDirectory() as directory:
            check = make_check(directory)
            items = [(echo.slot, item_for(echo)) for echo in check.build.echoes]
            observed_build(check.build, items)
            slot, item = items[0]
            items[0] = (slot, replace(item, identity=replace(item.identity, compatibility='cost_4:hp_percent')))
            with self.assertRaisesRegex(ValueError, 'main stat'):
                observed_build(check.build, items)

    def test_unresolved_scan_is_rejected_before_live_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            check = make_check(directory)
            validate_inspection(check)
            for change in ({'issues': ['OCR failed']}, {'stop_reason': 'COST 3: max page limit reached'}):
                changed = replace(check, inspection={'scan': dict(check.inspection['scan'], **change)})
                with self.assertRaisesRegex(ValueError, 'incomplete'):
                    validate_inspection(changed)

    def test_missing_stock_is_explicit_and_bound_to_current_check(self):
        with tempfile.TemporaryDirectory() as directory:
            check = make_check(directory)
            identity = check.candidate_pool[0]
            path = Path(directory) / 'missing.json'
            write_json(path, stock_document(check.source_path,
                [dict(identity=identity.__dict__, count=0, evidence='both orders inspected')]))
            self.assertEqual(load_missing_stock(path, check), ((identity, 'both orders inspected'),))
            Path(check.source_path).write_text(Path(check.source_path).read_text(encoding='utf-8') + '\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'exact inspection'):
                load_missing_stock(path, check)

    def test_agent_runner_reports_errors_and_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'run'
            runtime = SimpleNamespace(quit=Mock(), wait_task=Mock(), device_manager=SimpleNamespace(close=Mock()))
            with (patch.object(run_echo_workflow, 'create_runtime', return_value=runtime),
                  patch.object(run_echo_workflow, 'connect_runtime'),
                  patch.object(run_echo_workflow, 'run_operation', side_effect=RuntimeError('OCR failed after click')),
                  contextlib.redirect_stdout(io.StringIO())):
                code = run_echo_workflow.main(['observe', '--output', str(output)])
            report = json.loads((output / 'result.json').read_text(encoding='utf-8'))
            self.assertEqual(code, 1)
            self.assertEqual(report['status'], 'error')
            self.assertIn('OCR failed', report['error']['message'])
            runtime.quit.assert_called_once()
            runtime.device_manager.close.assert_called_once()

    def test_cleanup_failure_keeps_result_and_fails_process(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'run'
            runtime = SimpleNamespace(quit=Mock(side_effect=RuntimeError('close failed')),
                wait_task=Mock(), device_manager=SimpleNamespace(close=Mock()))
            with (patch.object(run_echo_workflow, 'create_runtime', return_value=runtime),
                  patch.object(run_echo_workflow, 'connect_runtime'), patch.object(run_echo_workflow, 'run_operation'),
                  contextlib.redirect_stdout(io.StringIO())):
                self.assertEqual(run_echo_workflow.main(['observe', '--output', str(output)]), 1)
            report = json.loads((output / 'result.json').read_text(encoding='utf-8'))
            self.assertEqual(report['cleanup_errors'][0]['step'], 'quit')
            runtime.device_manager.close.assert_called_once()

    def test_invalid_character_does_not_connect_to_game(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(run_echo_workflow, 'create_runtime') as create, contextlib.redirect_stdout(io.StringIO()):
                code = run_echo_workflow.main(['check', '--character', 'unknown', '--output', str(Path(directory) / 'run')])
            self.assertEqual(code, 1)
            create.assert_not_called()

    def test_help_runs_outside_checkout_without_game_runtime_dependencies(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            for script in ('run_echo_workflow.py', 'audit_echo_workflow.py', 'reconcile_echo_inventory.py'):
                # -S removes site-packages: even CLI help cannot rely on this
                # machine's OK runtime or ignored environment/config files.
                process = subprocess.run([sys.executable, '-S', str(root / 'scripts' / script), '--help'],
                    cwd=directory, capture_output=True, text=True, encoding='utf-8')
                self.assertEqual(process.returncode, 0, process.stderr)

    def test_stock_audit_page_limit_produces_no_missing_result(self):
        with tempfile.TemporaryDirectory() as directory:
            check = make_check(directory)
            item = item_for(check.build.echoes[0])
            task = SimpleNamespace(select_echo_sonata_set=Mock(), click_relative=Mock(),
                select_echo_inventory_level_descending=Mock(), sleep=Mock(), scroll_relative=Mock(),
                _read_selected_echo_inventory_item=Mock(return_value=item))
            with patch('src.echo.live_workflow.capture_snapshot'):
                with self.assertRaisesRegex(RuntimeError, 'page limit'):
                    audit_stock(task, check, directory, cost=4, max_pages=1)
            self.assertFalse((Path(directory) / 'verified_missing_stock.json').exists())

    def test_stock_audit_keeps_present_candidates_and_confirms_only_missing_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            check = make_check(directory)
            identity = next(identity for identity in check.candidate_pool if identity.cost == 3)
            item = replace(item_for(check.build.echoes[1], level=0), identity=identity)
            task = SimpleNamespace(select_echo_sonata_set=Mock(), click_relative=Mock(),
                select_echo_inventory_level_descending=Mock(), sleep=Mock(), scroll_relative=Mock(),
                _read_selected_echo_inventory_item=Mock(return_value=item))
            with patch('src.echo.live_workflow.capture_snapshot'):
                audit = audit_stock(task, check, directory, cost=3, max_pages=3)
            self.assertEqual(len(audit['directions']), 2)
            self.assertEqual(len(audit['fresh_items']), 1)
            missing = {tuple(row['identity'].values()) for row in audit['records']}
            expected = {tuple(candidate.__dict__.values()) for candidate in check.candidate_pool
                        if candidate.cost == 3 and candidate != identity}
            self.assertEqual(missing, expected)

    def test_live_observations_replace_the_old_helper_monkey_patch(self):
        with tempfile.TemporaryDirectory() as directory:
            check = make_check(directory)
            task = ModelEnhanceEchoTask.__new__(ModelEnhanceEchoTask)
            task._loaded_inspection_result = check
            identity = check.candidate_pool[0]
            task.configure_live_observations(missing_stock=((identity, 'verified'),), search_settle_time=1.5)
            session = AdaptiveEnhancementSession(check.build, check.candidate_pool,
                resources=EchoResourceSnapshot.manual(tuners=10, echo_exp=4400), config=check.policy)
            task._new_adaptive_session = Mock(return_value=session)
            task._resolve_resources = Mock()
            task._publish_resources = Mock()
            task._prepare_enhancement_runtime()
            self.assertEqual(session.remaining[identity], 0)
            self.assertEqual(task._live_search_settle_time, 1.5)


class TestPortableRunAudit(unittest.TestCase):
    def make_run(self, directory, check, *, interrupted=False):
        journal = DecisionJournal(Path(directory) / 'journal')
        session = AdaptiveEnhancementSession(check.build, check.candidate_pool,
            resources=EchoResourceSnapshot.manual(tuners=100, echo_exp=100000),
            config=check.policy, journal=journal,
            adaptive_policy=AdaptivePolicy(fresh_samples=10, maximum_samples=20))
        identity = check.candidate_pool[0]
        session.observe_stock(identity, 1, evidence='double-read stack', confirmed_available=True)
        session.begin_candidate(identity)
        session.unreconciled_upgrade = True
        session.emit('upgrade_click_intent', {})
        session.emit('upgrade_clicked', {})
        if not interrupted:
            session.record_reveal(EchoState(), EchoState((EchoRoll.from_value(EchoStat.HP_PERCENT, 6.4),)))
            session.unreconciled_upgrade = False
            session.emit('reveal_recorded', {})
            session.record_discard(identity, session.active_state, 'fixture')
        session.stop('fixture stop')
        return journal.directory / 'events.jsonl'

    def test_closed_journal_generates_text_report_and_exhaustion_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            check = make_check(directory)
            source = self.make_run(directory, check)
            report = audit_runs(check.source_path, [source])
            self.assertEqual(report['status'], 'completed')
            self.assertEqual(report['completed'], 1)
            self.assertEqual(report['modeled_resources']['gross_tuners'], 10)
            self.assertEqual(len(report['verified_missing_stock']['records']), 1)
            self.assertFalse(report['equipment_applied_in_game'])
            output = Path(directory) / 'audit'
            write_audit(output, report)
            self.assertTrue((output / 'summary.md').is_file())
            self.assertEqual(len(load_missing_stock(output / 'verified_missing_stock.json', check)), 1)

    def test_interrupted_click_does_not_generate_shortage_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            check = make_check(directory)
            report = audit_runs(check.source_path, [self.make_run(directory, check, interrupted=True)])
            self.assertEqual(report['status'], 'needs_reconciliation')
            self.assertEqual(report['verified_missing_stock']['records'], [])

    def test_duplicate_journal_cannot_double_count_consumption(self):
        with tempfile.TemporaryDirectory() as directory:
            check = make_check(directory)
            source = self.make_run(directory, check)
            with self.assertRaisesRegex(ValueError, 'Duplicate journal'):
                audit_runs(check.source_path, [source, source])

    def test_simulated_journal_cannot_generate_live_stock_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            check = make_check(directory)
            source = self.make_run(directory, check)
            events = [json.loads(line) for line in source.read_text(encoding='utf-8').splitlines()]
            for event in events:
                event['snapshot']['mode'] = 'simulation'
            source.write_text('\n'.join(json.dumps(event) for event in events), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'only live journals'):
                audit_runs(check.source_path, [source])

    def test_fresh_double_read_items_can_supplement_a_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            check = make_check(directory)
            item = item_for(check.build.echoes[0])
            self.assertEqual(inventory_item_from_dict(item.to_dict()), item)
            proof = Path(directory) / 'proof.json'
            report = dict(status='completed', phase='stock', stock_audit=dict(
                read_only=True, double_read_confirmed=True, inspection_sha256=check_digest(check.source_path),
                directions=[dict(reached_unchanged_fingerprint_end=True)] * 2, items=[item.to_dict()]))
            write_json(proof, report)
            output = Path(directory) / 'reconciled'
            updated = EchoInventoryCheckResult.read(reconcile(check.source_path, [proof], output))
            self.assertEqual(len(updated.completed_inventory()), 1)
            self.assertEqual(updated.build, check.build)
            report['stock_audit']['inspection_sha256'] = 'old check'
            write_json(proof, report)
            with self.assertRaisesRegex(ValueError, 'exact check'):
                reconcile(check.source_path, [proof], output)


if __name__ == '__main__':
    unittest.main()
