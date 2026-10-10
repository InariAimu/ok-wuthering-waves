import unittest
from unittest.mock import PropertyMock, patch
import numpy as np

from src.echo.api import EchoResourceSnapshot, ResourceSource
from src.echo.simulation import EchoState, EchoRoll, EchoStat
from src.task.ModelEnhanceEchoTask import ModelEnhanceEchoTask
from tests.test_echo_adaptive import session_fixture, fake_rankings


class TestAdaptiveLiveSearch(unittest.TestCase):
    def test_unreadable_stack_retains_only_reconciled_current_session_count(self):
        task = self.task()
        session = task._echo_enhancement_session
        identity = session.candidate_pool[0]
        session.observe_stock(identity, 2, evidence='two matching quantity reads', confirmed_available=True)
        session.begin_candidate(identity)
        session.record_reveal(EchoState(), EchoState((EchoRoll.from_value(EchoStat.HP_PERCENT, 6.4),)))
        session.record_discard(identity, session.active_state, 'test')
        session.observe_stock(identity, None, evidence='quantity unreadable', confirmed_available=True)
        self.assertEqual(session.remaining[identity], 1)
        session.observe_stock(identity, 4, evidence='new numeric read', confirmed_available=True)
        self.assertEqual(session.remaining[identity], 4)
        session.observe_stock(identity, 0, evidence='verified exhausted')
        session.observe_stock(identity, None, evidence='new available stack', confirmed_available=True)
        self.assertIsNone(session.remaining[identity])
    def test_scroll_detection_ignores_animation_between_cards(self):
        task = self.task()
        before = np.zeros((768, 1366, 3), dtype=np.uint8)
        after = before.copy()
        # The live 3C failure was caused by the animated scene between columns.
        after[110:690, 234:248] = 180
        task.next_frame = lambda: None
        task.scroll_relative = lambda *args: None
        task.sleep = lambda *args: None
        with patch.object(ModelEnhanceEchoTask, 'frame', new_callable=PropertyMock,
                          side_effect=(before, after)):
            self.assertFalse(ModelEnhanceEchoTask._adaptive_scroll_changed(task, 5, 0))

    def test_scroll_detection_preserves_card_level_changes(self):
        task = self.task()
        before = np.zeros((768, 1366, 3), dtype=np.uint8)
        after = before.copy()
        # A changed level/quantity label must remain evidence of movement,
        # even when the Echo thumbnails above it happen to be identical.
        after[190:204, 180:210] = 255
        task.next_frame = lambda: None
        task.scroll_relative = lambda *args: None
        task.sleep = lambda *args: None
        with patch.object(ModelEnhanceEchoTask, 'frame', new_callable=PropertyMock,
                          side_effect=(before, after)):
            self.assertTrue(ModelEnhanceEchoTask._adaptive_scroll_changed(task, 5, 0))

    def task(self):
        task = ModelEnhanceEchoTask.__new__(ModelEnhanceEchoTask)
        task._echo_enhancement_session = session_fixture()
        task.config = {'Max Candidate Search Pages': 1}
        task.info_set = lambda *args: None
        task.click_relative = lambda *args, **kwargs: None
        task.select_echo_inventory_level_descending = lambda **kwargs: None
        task._read_echo_card_level = lambda *args: 25
        task._adaptive_scroll_changed = lambda *args: False
        return task

    def test_verified_end_in_both_orders_marks_shortage(self):
        task = self.task()
        identity = task._echo_enhancement_session.candidate_pool[1]
        self.assertFalse(task._find_adaptive_fresh_echo(identity, 0))
        self.assertEqual(task._echo_enhancement_session.remaining[identity], 0)

    def test_page_limit_is_inconclusive_not_shortage(self):
        task = self.task()
        task._adaptive_scroll_changed = lambda *args: True
        identity = task._echo_enhancement_session.candidate_pool[1]
        with self.assertRaisesRegex(RuntimeError, '未完整确认'):
            task._find_adaptive_fresh_echo(identity, 0)
        self.assertIsNone(task._echo_enhancement_session.remaining[identity])

    def test_ocr_failure_is_inconclusive_not_shortage(self):
        task = self.task()
        task._read_echo_card_level = lambda *args: None
        task._read_selected_echo_level = lambda: 0
        def fail():
            raise ValueError('unreadable name')
        task._read_current_echo_candidate_identity = fail
        identity = task._echo_enhancement_session.candidate_pool[1]
        with self.assertRaisesRegex(RuntimeError, '未完整确认'):
            task._find_adaptive_fresh_echo(identity, 0)
        self.assertIsNone(task._echo_enhancement_session.remaining[identity])

    def test_exact_target_and_stack_are_observed_before_start(self):
        task = self.task()
        session = task._echo_enhancement_session
        identity = session.candidate_pool[1]
        task._read_echo_card_level = lambda *args: None
        task._read_selected_echo_level = lambda: 0
        task._read_current_echo_candidate_identity = lambda: identity
        task._read_adaptive_stack_count = lambda *args: 10
        self.assertTrue(task._find_adaptive_fresh_echo(identity, 0))
        self.assertEqual(task._selected_model_candidate_identity, identity)
        self.assertEqual(session.remaining[identity], 10)
        self.assertIsNone(session.active_identity)
        self.assertEqual(session.ledger.gross_tuners, 0)

    def test_scheduler_searches_four_cost_after_three_cost_is_missing(self):
        task = self.task()
        session = task._echo_enhancement_session
        body, gate, tower = session.candidate_pool
        session.rankings = lambda *args: fake_rankings(session, [.328, 1, .936],
            [session.remaining[identity] for identity in (body, gate, tower)])
        searched = []
        def search(identity, settle_time):
            searched.append(identity)
            session.observe_stock(identity, 0 if identity == gate else 1, evidence='test')
            return identity != gate
        task._find_adaptive_fresh_echo = search
        self.assertEqual(task._select_first_fresh_echo(0), 0)
        self.assertEqual(searched, [gate, tower])
        self.assertEqual(session.active_identity, tower)
        self.assertEqual(session.ledger.gross_tuners, 0)

    def test_upgrade_click_and_readback_have_separate_audit_states(self):
        task = self.task()
        session = task._echo_enhancement_session
        task._model_ui_event('upgrade_click_intent', target_slots=1)
        self.assertTrue(session.unreconciled_upgrade)
        task._model_ui_event('upgrade_clicked', target_slots=1)
        self.assertTrue(session.unreconciled_upgrade)
        task._model_ui_event('reveal_recorded', target_slots=1)
        self.assertFalse(session.unreconciled_upgrade)
        self.assertEqual([event['kind'] for event in session.events[-3:]],
                         ['upgrade_click_intent', 'upgrade_clicked', 'reveal_recorded'])

    def test_failed_resource_reader_does_not_restore_initial_manual_budget(self):
        task = self.task()
        session = task._echo_enhancement_session
        identity = session.candidate_pool[0]
        session.begin_candidate(identity)
        revealed = EchoState((EchoRoll.from_value(EchoStat.HP_PERCENT, 6.4),))
        session.record_reveal(EchoState(), revealed)
        session.record_discard(identity, revealed, 'test')
        before = session.ledger.snapshot()
        task._echo_resource_provider = object()
        task._last_resource_sync_completed = 0
        task._resolve_resources = lambda: EchoResourceSnapshot(1000, 1000000, ResourceSource.MANUAL_FALLBACK, 'OCR failed')
        task._find_adaptive_fresh_echo = lambda *args: True
        task._select_first_fresh_echo(0)
        self.assertEqual(session.ledger.snapshot(), before)
        self.assertIn('resource_read_unavailable', [event['kind'] for event in session.events])

    def test_successful_resource_reader_updates_balance_without_erasing_costs(self):
        task = self.task()
        session = task._echo_enhancement_session
        task._echo_resource_provider = object()
        task._last_resource_sync_completed = -1
        task._resolve_resources = lambda: EchoResourceSnapshot(500, 500000, ResourceSource.OK_WW, 'readback')
        task._find_adaptive_fresh_echo = lambda *args: True
        task._select_first_fresh_echo(0)
        self.assertEqual(session.ledger.tuners, 500)
        self.assertEqual(session.ledger.gross_tuners, 0)
        self.assertIn('resource_observation', [event['kind'] for event in session.events])
