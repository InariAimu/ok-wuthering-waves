import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from src.echo.adaptive import AdaptiveEnhancementSession, AdaptivePolicy, CandidateRanking
from src.echo.api import EchoResourceSnapshot, EnhancementAction
from src.echo.decision_log import DecisionJournal
from src.echo.joint import JointInventory
from src.echo.optimizer import CompletionMetrics, EchoIdentity, NamedEchoBuild, NamedEquippedEcho, PolicyConfig
from src.echo.panel import CharacterPanel, CharacterScoringProfile, EnergyRegenPolicy
from src.echo.simulation import EchoRoll, EchoState, EchoStat, EchoUpgradeSimulator


def state(*pairs):
    return EchoState(tuple(EchoRoll.from_value(stat, value) for stat, value in pairs))


def full_state(*pairs):
    filler = ((EchoStat.HP_FLAT, 320), (EchoStat.DEFENSE_FLAT, 40),
              (EchoStat.HP_PERCENT, 6.4), (EchoStat.DEFENSE_PERCENT, 8.1),
              (EchoStat.RESONANCE_SKILL_DAMAGE, 6.4))
    stats = {stat for stat, _ in pairs}
    return state(*(tuple(pairs)+tuple(pair for pair in filler if pair[0] not in stats))[:5])


def session_fixture(**kwargs):
    identities = tuple(EchoIdentity(name, cost) for name, cost in (('body', 1), ('gate', 3), ('tower', 4)))
    build = NamedEchoBuild(CharacterScoringProfile('test', {EchoStat.CRIT_RATE: 1.8, EchoStat.CRIT_DAMAGE: .9}),
        CharacterPanel.base(), tuple(NamedEquippedEcho(str(i), identity, full_state()) for i, identity in enumerate(identities)))
    defaults = dict(resources=EchoResourceSnapshot.manual(tuners=1000, echo_exp=1000000),
                    config=PolicyConfig(rollout_samples=100),
                    adaptive_policy=AdaptivePolicy(fresh_samples=100, maximum_samples=400))
    defaults.update(kwargs)
    return AdaptiveEnhancementSession(build, identities, **defaults)


def fake_rankings(session, efficiencies, counts=None, width=0):
    best = max(efficiencies)
    rows = []
    for identity, efficiency, count in zip(session.candidate_pool, efficiencies, counts or [10]*3):
        metric = CompletionMetrics(.1, efficiency, efficiency*10, 35, 50000, 1, efficiency, (), 100)
        rows.append(CandidateRanking(identity, count, 'missing' if count == 0 else 'present', metric,
            efficiency/best if best else 0, width/1.96, (max(0, efficiency-width), efficiency+width)))
    return tuple(sorted(rows, key=lambda row: row.metrics.efficiency, reverse=True))


class TestAdaptiveScheduling(unittest.TestCase):
    def test_prefers_three_cost_when_it_is_best(self):
        session = session_fixture()
        session.rankings = lambda *args: fake_rankings(session, [.007, 1, .936])
        self.assertEqual(session.choose_candidate().selected.name, 'gate')

    def test_missing_three_cost_falls_back_to_close_four_cost(self):
        session = session_fixture()
        session.rankings = lambda *args: fake_rankings(session, [.007, 1, .936], [29, 0, 5])
        self.assertEqual(session.choose_candidate().selected.name, 'tower')

    def test_stops_instead_of_spending_on_weak_leftovers(self):
        session = session_fixture()
        session.rankings = lambda *args: fake_rankings(session, [.328, 1, .936], [80, 0, 0])
        selection = session.choose_candidate()
        self.assertEqual(selection.action, 'stop_missing_stock')
        self.assertIsNone(selection.selected)
        self.assertEqual(selection.best_legal.name, 'gate')

    def test_unknown_stock_is_checked_before_declaring_shortage(self):
        session = session_fixture()
        session.rankings = lambda *args: fake_rankings(session, [.328, 1, .936], [80, None, 0])
        self.assertEqual(session.choose_candidate().selected.name, 'gate')

    def test_switch_hysteresis_and_boundary(self):
        session = session_fixture()
        session.previous_identity = session.candidate_pool[0]
        session.rankings = lambda *args: fake_rankings(session, [80, 99, 0])
        self.assertEqual(session.choose_candidate().selected.name, 'body')
        session.rankings = lambda *args: fake_rankings(session, [80, 100, 0])
        self.assertEqual(session.choose_candidate().selected.name, 'gate')

    def test_hysteresis_cannot_keep_candidate_below_missing_floor(self):
        session = session_fixture()
        session.previous_identity = session.candidate_pool[0]
        session.rankings = lambda *args: fake_rankings(session, [49, 100, 51], [10, 0, 10])
        self.assertEqual(session.choose_candidate().selected.name, 'tower')

    def test_exact_half_without_estimation_uncertainty_is_allowed(self):
        session = session_fixture()
        session.rankings = lambda *args: fake_rankings(session, [50, 100, 93], [10, 0, 0])
        self.assertEqual(session.choose_candidate().selected.name, 'body')

    def test_refines_uncertain_stop_then_stops_for_review(self):
        session = session_fixture()
        calls = []
        def rankings(samples=None):
            calls.append(samples)
            return fake_rankings(session, [49, 100, 93], [10, 0, 0], width=5)
        session.rankings = rankings
        decision = session.choose_candidate()
        self.assertEqual(calls, [None, 400])
        self.assertTrue(decision.refined)
        self.assertEqual(decision.action, 'stop_uncertain')

    def test_all_zero_samples_are_not_reported_as_impossibility(self):
        session = session_fixture()
        session.rankings = lambda *args: fake_rankings(session, [0, 0, 0])
        self.assertEqual(session.choose_candidate().action, 'stop_no_sampled_gain')

    def test_invalid_thresholds_are_rejected(self):
        for kwargs in ({'switch_efficiency_ratio': .9}, {'missing_candidate_stop_ratio': 0},
                       {'fresh_samples': 1}, {'maximum_samples': 10}, {'switch_efficiency_ratio': float('nan')}):
            with self.assertRaises(ValueError):
                AdaptivePolicy(**kwargs)


class TestAdaptiveAccounting(unittest.TestCase):
    def test_expected_refunds_reduce_economic_cost_without_crediting_cash(self):
        session = session_fixture()
        identity = session.candidate_pool[0]
        session.observe_stock(identity, 2, evidence='test')
        session.begin_candidate(identity)
        decision = session.advise(identity, EchoState())
        self.assertLess(decision.metrics.expected_relative_tuners, 50)
        self.assertLess(decision.metrics.expected_relative_exp, 142600)
        observed = state((EchoStat.HP_PERCENT, 6.4))
        session.record_reveal(EchoState(), observed)
        cash = session.ledger.snapshot()
        session.record_discard(identity, observed, 'test')
        self.assertEqual(session.ledger.snapshot(), cash)
        self.assertEqual(session.snapshot().recoverable_tuners_later, 3)
        self.assertEqual(session.remaining[identity], 1)
        self.assertEqual(session.audit_snapshot()['resources']['economic_net_tuners'], 7)

    def test_stack_is_consumed_only_on_first_reveal(self):
        session = session_fixture()
        identity = session.candidate_pool[0]
        session.observe_stock(identity, 1, evidence='test')
        session.begin_candidate(identity)
        first = state((EchoStat.CRIT_RATE, 6.3))
        second = first.add(EchoRoll.from_value(EchoStat.CRIT_DAMAGE, 12.6))
        session.record_reveal(EchoState(), first)
        session.record_reveal(first, second)
        self.assertEqual(session.remaining[identity], 0)
        self.assertEqual(session.stock_status[identity], 'missing')

    def test_unaffordable_first_step_does_not_consume_stock(self):
        session = session_fixture(resources=EchoResourceSnapshot.manual(tuners=0, echo_exp=0))
        identity = session.candidate_pool[0]
        session.observe_stock(identity, 1, evidence='test')
        session.begin_candidate(identity)
        self.assertEqual(session.advise(identity, EchoState()).action, EnhancementAction.STOP_RESOURCES)
        self.assertEqual(session.remaining[identity], 1)
        self.assertFalse(session.pending_discards)

    def test_scheduler_stops_before_search_when_first_step_is_unaffordable(self):
        session = session_fixture(resources=EchoResourceSnapshot.manual(tuners=0, echo_exp=0))
        session.rankings = lambda *args: self.fail('should not need expensive sampling without materials')
        self.assertEqual(session.choose_candidate().action, 'stop_resources')

    def test_reserve_retention_is_not_counted_as_expected_refund(self):
        session = session_fixture()
        identity = session.candidate_pool[0]
        new = type(identity)('new-name', identity.cost, identity.compatibility)
        session.observe_stock(new, 1, evidence='test')
        row = next(row for row in session.rankings() if row.identity == new)
        self.assertEqual(row.retained_probability, 1)
        self.assertEqual(row.metrics.expected_relative_tuners, 50)
        self.assertEqual(row.metrics.expected_relative_exp, 142600)

    def test_logs_remain_readable_after_interrupted_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            journal = DecisionJournal(Path(temporary)/'run')
            session = session_fixture(journal=journal)
            session.choose_candidate()
            identity = session.candidate_pool[0]
            session.begin_candidate(identity)
            revealed = state((EchoStat.CRIT_RATE, 6.3))
            session.record_reveal(EchoState(), revealed)
            session.stop('OCR failed', error='unreadable next state')
            events = [json.loads(line) for line in (journal.directory/'events.jsonl').read_text(encoding='utf-8').splitlines()]
            latest = json.loads((journal.directory/'latest.json').read_text(encoding='utf-8'))
            self.assertEqual([e['sequence'] for e in events], list(range(1, len(events)+1)))
            self.assertEqual(latest['event']['snapshot']['resources']['gross_tuners'], 10)
            self.assertEqual(latest['event']['snapshot']['active_state']['rolls'][0]['stat'], 'crit_rate')
            self.assertIsNotNone(latest['latest_ranking'])
            self.assertIn('未回收返还不计入', (journal.directory/'summary.md').read_text(encoding='utf-8'))

    def test_new_compatible_name_can_be_discovered_without_guessing_its_quantity(self):
        session = session_fixture()
        identity = EchoIdentity('new-one-cost', 1)
        session.observe_stock(identity, None, evidence='two matching detail reads', confirmed_available=True)
        self.assertIn(identity, session.candidate_pool)
        self.assertIsNone(session.remaining[identity])
        self.assertEqual(session.stock_status[identity], 'present')


class TestJointFullCandidates(unittest.TestCase):
    def setUp(self):
        self.a, self.b = EchoIdentity('a', 3), EchoIdentity('b', 1)
        profile = CharacterScoringProfile('joint', {EchoStat.CRIT_RATE: 1.8, EchoStat.CRIT_DAMAGE: .9, EchoStat.ENERGY_REGEN: .5},
            energy_policy=EnergyRegenPolicy(120, 135, 145, tolerance_weight_ratio=.5))
        self.build = NamedEchoBuild(profile, CharacterPanel.base(energy_regen=110), (
            NamedEquippedEcho('3C', self.a, full_state((EchoStat.ENERGY_REGEN, 10.8))),
            NamedEquippedEcho('1C', self.b, full_state((EchoStat.CRIT_RATE, 6.3)))))
        self.alternate = NamedEquippedEcho('alternate', self.a,
            full_state((EchoStat.CRIT_RATE, 10.5), (EchoStat.CRIT_DAMAGE, 21)))

    def make_session(self):
        return AdaptiveEnhancementSession(self.build, (self.a, self.b),
            completed_inventory=(self.alternate,),
            resources=EchoResourceSnapshot.manual(tuners=1000, echo_exp=1000000),
            config=PolicyConfig(rollout_samples=100),
            adaptive_policy=AdaptivePolicy(fresh_samples=100, maximum_samples=400))

    def test_any_full_candidate_runs_joint_optimization_and_unlocks_old_inventory(self):
        session = self.make_session()
        candidate = full_state((EchoStat.CRIT_RATE, 6.3), (EchoStat.ENERGY_REGEN, 10.8))
        session.begin_candidate(self.b)
        session.record_reveal(EchoState(), candidate)
        decision = session.advise(self.b, candidate)
        self.assertEqual(decision.action, EnhancementAction.KEEP_AND_LOCK)
        self.assertGreater(decision.score_gain, 30)
        session.record_keep(self.b, candidate, decision.replacement_slot)
        self.assertEqual(session.build.echoes[0].state, self.alternate.state)
        self.assertEqual(sum(e['kind'] == 'joint_optimization' for e in session.events), 1)

    def test_duplicate_full_candidate_still_logs_joint_check(self):
        session = self.make_session()
        candidate = self.build.echoes[1].state
        session.begin_candidate(self.b)
        session.record_reveal(EchoState(), candidate)
        decision = session.advise(self.b, candidate)
        self.assertEqual(decision.action, EnhancementAction.MARK_DISCARD)
        self.assertEqual(sum(e['kind'] == 'joint_optimization' for e in session.events), 1)

    def test_joint_sample_envelope_matches_brute_force_across_er_bands(self):
        inventory = JointInventory(self.build, (self.alternate,))
        simulator = EchoUpgradeSimulator(seed=15)
        samples = tuple(simulator.upgrade_to(EchoState(), 5).final for _ in range(30))
        for identity in (self.a, self.b, EchoIdentity('new', 1)):
            gains, _ = inventory.gains(self.build, identity, samples)
            for sample, gain in zip(samples, gains):
                brute = JointInventory(self.build, (self.alternate, NamedEquippedEcho('new', identity, sample)))
                self.assertAlmostEqual(gain, brute.optimize(self.build).score-self.build.score)

    def test_joint_samples_support_capped_er_legacy_profile(self):
        build = replace(self.build, profile=CharacterScoringProfile('cap', {EchoStat.CRIT_RATE: 1.8, EchoStat.ENERGY_REGEN: .5}, energy_regen_target=120))
        inventory = JointInventory(build)
        sample = full_state((EchoStat.CRIT_RATE, 10.5), (EchoStat.ENERGY_REGEN, 10.8))
        gains, _ = inventory.gains(build, self.b, (sample,))
        brute = JointInventory(build, (NamedEquippedEcho('new', self.b, sample),))
        self.assertAlmostEqual(gains[0], brute.optimize(build).score-build.score)
