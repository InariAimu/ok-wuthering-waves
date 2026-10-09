from fractions import Fraction
import unittest

from src.echo.simulation import (
    ALL_STATS,
    ROLL_TABLE,
    EchoRoll,
    EchoState,
    EchoStat,
    EchoUpgradeSimulator,
    ResourceAmount,
    ResourceCostModel,
    next_roll_distribution,
    roll_tier_distribution,
)


class TestPublishedRollTable(unittest.TestCase):
    def test_all_thirteen_stats_are_present(self):
        self.assertEqual(len(ALL_STATS), 13)
        self.assertEqual(set(ROLL_TABLE), set(ALL_STATS))

    def test_published_crit_and_flat_attack_distributions(self):
        crit = ROLL_TABLE[EchoStat.CRIT_RATE]
        self.assertEqual([tier.value for tier in crit], [6.3, 6.9, 7.5, 8.1, 8.7, 9.3, 9.9, 10.5])
        self.assertEqual([tier.weight for tier in crit], [70, 70, 70, 24, 24, 24, 9, 9])

        flat_attack = ROLL_TABLE[EchoStat.ATTACK_FLAT]
        self.assertEqual([tier.value for tier in flat_attack], [30, 40, 50, 60])
        self.assertEqual([tier.weight for tier in flat_attack], [7, 54, 39, 3])

        normalized_crit = roll_tier_distribution(EchoStat.CRIT_RATE)
        self.assertEqual(sum((probability for _, probability in normalized_crit), Fraction()), Fraction(1))
        self.assertEqual(normalized_crit[0][1], Fraction(7, 30))
        self.assertEqual(normalized_crit[-1][1], Fraction(3, 100))

    def test_next_roll_distribution_is_exact_and_excludes_existing_stats(self):
        state = EchoState((EchoRoll(EchoStat.CRIT_RATE, 0),))
        distribution = next_roll_distribution(state)

        self.assertEqual(sum((item.probability for item in distribution), Fraction()), Fraction(1))
        self.assertNotIn(EchoStat.CRIT_RATE, {item.roll.stat for item in distribution})

        attack_probability = sum(
            item.probability
            for item in distribution
            if item.roll.stat == EchoStat.ATTACK_PERCENT
        )
        self.assertEqual(attack_probability, Fraction(1, 12))

    def test_observed_value_can_be_mapped_back_to_a_public_tier(self):
        roll = EchoRoll.from_value(EchoStat.CRIT_DAMAGE, 13.8)
        self.assertEqual(roll.tier, 1)
        self.assertEqual(roll.value, 13.8)


class TestEchoSimulation(unittest.TestCase):
    def test_seeded_full_upgrade_is_reproducible_and_has_unique_stats(self):
        first = EchoUpgradeSimulator(seed=151).upgrade_to(
            EchoState(), 5, consume_echo=True
        )
        second = EchoUpgradeSimulator(seed=151).upgrade_to(
            EchoState(), 5, consume_echo=True
        )

        self.assertEqual(first.final, second.final)
        self.assertEqual(len(first.final.stats), 5)
        self.assertEqual(first.final.level, 25)
        self.assertEqual(first.gross_cost, second.gross_cost)

    def test_upgrade_can_continue_from_a_partial_echo(self):
        partial = EchoState((
            EchoRoll(EchoStat.CRIT_RATE, 0),
            EchoRoll(EchoStat.ATTACK_PERCENT, 3),
        ))
        result = EchoUpgradeSimulator(seed=2).upgrade_to(partial, 4)

        self.assertEqual(result.initial, partial)
        self.assertEqual(result.final.rolls[:2], partial.rolls)
        self.assertEqual(len(result.revealed), 2)
        self.assertNotIn(EchoStat.CRIT_RATE, {roll.stat for roll in result.revealed})
        self.assertNotIn(EchoStat.ATTACK_PERCENT, {roll.stat for roll in result.revealed})
        self.assertEqual(result.gross_cost.tuners, 20)
        self.assertEqual(result.gross_cost.echo_exp, 62_600)


class TestResourceCostModel(unittest.TestCase):
    def setUp(self):
        self.costs = ResourceCostModel()

    def test_cumulative_and_incremental_upgrade_costs(self):
        full = self.costs.invested_cost(5)
        self.assertEqual(full.echoes, 1)
        self.assertEqual(full.tuners, 50)
        self.assertEqual(full.echo_exp, 142_600)
        self.assertEqual(full.premium_tubes, 28.52)
        self.assertEqual(full.shell_credits, 24_260)

        plus_10_to_plus_15 = self.costs.upgrade_cost(2, 3)
        self.assertEqual(plus_10_to_plus_15.tuners, 10)
        self.assertEqual(plus_10_to_plus_15.echo_exp, 23_100)
        self.assertEqual(plus_10_to_plus_15.shell_credits, 4_310)

    def test_discarded_attempt_subtracts_tuner_and_exp_refunds(self):
        refund = self.costs.recycle_refund(5)
        self.assertEqual(refund.tuners, 15)
        self.assertEqual(refund.echo_exp, 94_116)

        discarded = self.costs.attempt_cost(5, keep=False)
        self.assertEqual(discarded.echoes, 1)
        self.assertEqual(discarded.tuners, 35)
        self.assertEqual(discarded.echo_exp, 48_484)
        self.assertEqual(discarded.shell_credits, 24_260)

    def test_inventory_can_ignore_resources_assumed_unlimited(self):
        inventory = ResourceAmount(tuners=50, echo_exp=142_600)
        full_cost = self.costs.invested_cost(5)
        self.assertFalse(inventory.covers(full_cost))
        self.assertTrue(inventory.covers(
            full_cost,
            ignore_echoes=True,
            ignore_shell_credits=True,
        ))

    def test_refund_ratio_is_bounded_by_theoretical_maximum(self):
        with self.assertRaises(ValueError):
            ResourceCostModel(exp_refund_ratio=0.751)


if __name__ == "__main__":
    unittest.main()
