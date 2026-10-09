import unittest

from src.echo.benchmark import (
    FirstCritCommitPolicy,
    OpportunityCostPolicy,
    PublishedHeuristicPolicy,
    candidate_stream,
    empty_build,
    run_fixed_attempt_benchmark,
)
from src.echo.panel import MainDpsWeightClass
from src.echo.scenarios import xuanling_scenario
from src.echo.simulation import EchoRoll, EchoState, EchoStat


def echo_with(*rolls: tuple[EchoStat, float]) -> EchoState:
    return EchoState(tuple(EchoRoll.from_value(stat, value) for stat, value in rolls))


class TestPublishedHeuristic(unittest.TestCase):
    def setUp(self):
        scenario = xuanling_scenario(
            required_er=120,
            comfort_er=125,
            overflow_er=130,
            weight_class=MainDpsWeightClass.TYPE_1,
        )
        self.build = empty_build(scenario.build)
        self.identity = next(item for item in scenario.candidate_pool if item.cost == 1)
        self.policy = PublishedHeuristicPolicy()

    def test_a_rejects_ineffective_first_line(self):
        state = echo_with((EchoStat.HP_PERCENT, 10.9))
        self.assertIsNone(self.policy.decision(self.build, self.identity, state).next_slots)

    def test_b2_accepts_only_high_single_roll(self):
        high = echo_with(
            (EchoStat.CRIT_RATE, 9.3),
            (EchoStat.HP_PERCENT, 10.9),
            (EchoStat.DEFENSE_PERCENT, 10.9),
        )
        low = echo_with(
            (EchoStat.CRIT_RATE, 8.7),
            (EchoStat.HP_PERCENT, 10.9),
            (EchoStat.DEFENSE_PERCENT, 10.9),
        )
        self.assertEqual(self.policy.decision(self.build, self.identity, high).next_slots, 4)
        self.assertIsNone(self.policy.decision(self.build, self.identity, low).next_slots)

    def test_c3_attack_percent_table_boundary(self):
        passing = echo_with(
            (EchoStat.ATTACK_PERCENT, 7.9),
            (EchoStat.CRIT_RATE, 8.1),
            (EchoStat.HP_PERCENT, 10.9),
            (EchoStat.DEFENSE_PERCENT, 10.9),
        )
        failing = echo_with(
            (EchoStat.ATTACK_PERCENT, 7.9),
            (EchoStat.CRIT_RATE, 7.5),
            (EchoStat.HP_PERCENT, 10.9),
            (EchoStat.DEFENSE_PERCENT, 10.9),
        )
        self.assertEqual(
            self.policy.decision(self.build, self.identity, passing).reason,
            "C3_table_threshold",
        )
        self.assertIsNone(self.policy.decision(self.build, self.identity, failing).next_slots)

    def test_c3_primary_damage_and_flat_attack_boundaries(self):
        heavy = echo_with(
            (EchoStat.HEAVY_ATTACK_DAMAGE, 9.4),
            (EchoStat.CRIT_DAMAGE, 18.6),
            (EchoStat.HP_PERCENT, 10.9),
            (EchoStat.DEFENSE_PERCENT, 10.9),
        )
        flat = echo_with(
            (EchoStat.ATTACK_FLAT, 50),
            (EchoStat.CRIT_RATE, 9.9),
            (EchoStat.HP_PERCENT, 10.9),
            (EchoStat.DEFENSE_PERCENT, 10.9),
        )
        self.assertEqual(
            self.policy.decision(self.build, self.identity, heavy).reason,
            "C3_table_threshold",
        )
        self.assertEqual(
            self.policy.decision(self.build, self.identity, flat).reason,
            "C3_table_threshold",
        )

    def test_d_requires_double_crit_and_threshold_or_four_effective(self):
        score_pass = echo_with(
            (EchoStat.CRIT_RATE, 10.5),
            (EchoStat.CRIT_DAMAGE, 21.0),
            (EchoStat.ATTACK_PERCENT, 6.4),
            (EchoStat.HP_PERCENT, 10.9),
            (EchoStat.DEFENSE_PERCENT, 10.9),
        )
        no_double_crit = echo_with(
            (EchoStat.CRIT_RATE, 10.5),
            (EchoStat.ATTACK_PERCENT, 11.6),
            (EchoStat.HEAVY_ATTACK_DAMAGE, 11.6),
            (EchoStat.ATTACK_FLAT, 60),
            (EchoStat.ENERGY_REGEN, 12.4),
        )
        self.assertIsNotNone(self.policy.replacement(self.build, self.identity, score_pass))
        self.assertIsNone(self.policy.replacement(self.build, self.identity, no_double_crit))


class TestFixedAttemptBenchmark(unittest.TestCase):
    def test_first_crit_commit_uses_equivalent_crit_scale(self):
        delegate = OpportunityCostPolicy(rollout_samples=10)
        policy = FirstCritCommitPolicy(delegate, equivalent_threshold=19.8)
        scenario = xuanling_scenario(
            required_er=120,
            comfort_er=125,
            overflow_er=130,
            weight_class=MainDpsWeightClass.TYPE_1,
        )
        build = empty_build(scenario.build)
        identity = scenario.candidate_pool[0]
        self.assertEqual(
            policy.decision(
                build, identity, echo_with((EchoStat.CRIT_RATE, 9.9))
            ).next_slots,
            5,
        )
        self.assertEqual(
            policy.decision(
                build, identity, echo_with((EchoStat.CRIT_DAMAGE, 19.8))
            ).next_slots,
            5,
        )
        self.assertNotEqual(
            policy.decision(
                build, identity, echo_with((EchoStat.CRIT_RATE, 9.3))
            ).reason,
            "first_crit_commit",
        )

    def test_candidate_stream_has_43311_attempt_ratio_and_is_reproducible(self):
        scenario = xuanling_scenario()
        first = candidate_stream(scenario.candidate_pool, attempts=10, seed=1)
        second = candidate_stream(scenario.candidate_pool, attempts=10, seed=1)
        self.assertEqual(first, second)
        self.assertEqual(
            [outcome.identity.cost for outcome in first],
            [4, 3, 3, 1, 1, 4, 3, 3, 1, 1],
        )

    def test_run_records_unlimited_resource_costs(self):
        scenario = xuanling_scenario(
            required_er=120,
            comfort_er=125,
            overflow_er=130,
            weight_class=MainDpsWeightClass.TYPE_1,
        )
        build = empty_build(scenario.build)
        outcomes = candidate_stream(scenario.candidate_pool, attempts=20, seed=7)
        result = run_fixed_attempt_benchmark(
            build, outcomes, PublishedHeuristicPolicy()
        )
        self.assertEqual(result.attempts, 20)
        self.assertEqual(result.kept + result.discarded, 20)
        self.assertGreater(result.ledger.gross.tuners, 0)
        self.assertGreaterEqual(result.ledger.net.tuners, 0)
        self.assertEqual(len(result.score_curve), 20)


if __name__ == "__main__":
    unittest.main()
