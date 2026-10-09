import math
import unittest

from src.echo import (
    CharacterBuild,
    CharacterPanel,
    CharacterScoringProfile,
    EchoRoll,
    EchoState,
    EchoStat,
    EnergyRegenPolicy,
    MainDpsWeightClass,
    EquippedEcho,
    estimate_continued_improvement,
)


def echo_with(*rolls: tuple[EchoStat, float]) -> EchoState:
    return EchoState(tuple(EchoRoll.from_value(stat, value) for stat, value in rolls))


class TestCharacterScoringProfile(unittest.TestCase):
    def setUp(self):
        self.profile = CharacterScoringProfile.main_dps(energy_regen_target=130)
        self.empty_panel = CharacterPanel.base()

    def test_default_main_dps_ordering_is_enforced_across_public_rolls(self):
        crit_rate = self.profile.marginal_score(self.empty_panel, EchoStat.CRIT_RATE, 6.3)
        crit_damage = self.profile.marginal_score(self.empty_panel, EchoStat.CRIT_DAMAGE, 12.6)
        best_attack = self.profile.marginal_score(self.empty_panel, EchoStat.ATTACK_PERCENT, 11.6)
        best_liberation = self.profile.marginal_score(
            self.empty_panel, EchoStat.RESONANCE_LIBERATION_DAMAGE, 11.6
        )

        self.assertAlmostEqual(crit_rate, crit_damage)
        self.assertGreater(crit_rate, best_attack)
        self.assertGreater(best_attack, best_liberation)

    def test_energy_regen_only_scores_until_the_configured_target(self):
        almost_capped = CharacterPanel.base(energy_regen=129)
        capped = CharacterPanel.base(energy_regen=130)

        self.assertAlmostEqual(
            self.profile.marginal_score(almost_capped, EchoStat.ENERGY_REGEN, 6.8),
            0.5,
        )
        self.assertAlmostEqual(
            self.profile.marginal_score(capped, EchoStat.ENERGY_REGEN, 12.4),
            0,
        )
        er_table = self.profile.marginal_roll_table(capped)[EchoStat.ENERGY_REGEN]
        self.assertEqual(sum((entry.probability for entry in er_table), start=0), 1)
        self.assertTrue(all(entry.score == 0 for entry in er_table))

    def test_character_specific_weights_can_override_generic_damage_types(self):
        profile = CharacterScoringProfile.main_dps(
            name="liberation_character",
            overrides={
                EchoStat.RESONANCE_LIBERATION_DAMAGE: 0.7,
                EchoStat.BASIC_ATTACK_DAMAGE: 0.1,
            },
        )
        panel = CharacterPanel.base()
        liberation = profile.marginal_score(
            panel, EchoStat.RESONANCE_LIBERATION_DAMAGE, 10.1
        )
        basic = profile.marginal_score(panel, EchoStat.BASIC_ATTACK_DAMAGE, 10.1)
        self.assertAlmostEqual(liberation, basic * 7)

    def test_main_dps_overrides_cannot_invert_the_generic_priority(self):
        with self.assertRaises(ValueError):
            CharacterScoringProfile.main_dps(
                overrides={EchoStat.RESONANCE_LIBERATION_DAMAGE: 1.0}
            )

    def test_normalized_coverage_makes_full_coverage_atk_equivalent(self):
        profile = CharacterScoringProfile.normalized_main_dps(
            "heavy_only",
            damage_coverage={EchoStat.HEAVY_ATTACK_DAMAGE: 1.0},
            energy_policy=EnergyRegenPolicy(130, 135, 145),
        )
        panel = CharacterPanel.base(energy_regen=130)
        attack = profile.marginal_score(panel, EchoStat.ATTACK_PERCENT, 11.6)
        heavy = profile.marginal_score(panel, EchoStat.HEAVY_ATTACK_DAMAGE, 11.6)
        liberation = profile.marginal_score(
            panel, EchoStat.RESONANCE_LIBERATION_DAMAGE, 11.6
        )
        self.assertAlmostEqual(attack, heavy)
        self.assertEqual(liberation, 0)

    def test_three_band_energy_policy_is_continuous_and_enforces_requirement(self):
        energy = EnergyRegenPolicy(required=130, comfort=135, overflow=145)
        profile = CharacterScoringProfile(
            "three_band_er",
            {EchoStat.ENERGY_REGEN: 0.5},
            energy_policy=energy,
        )

        below = CharacterPanel.base(energy_regen=129.9)
        required = CharacterPanel.base(energy_regen=130)
        comfort = CharacterPanel.base(energy_regen=135)
        tolerance = CharacterPanel.base(energy_regen=140)
        overflow = CharacterPanel.base(energy_regen=145)

        self.assertFalse(profile.is_feasible(below))
        self.assertTrue(profile.is_feasible(required))
        self.assertAlmostEqual(profile.score(comfort), 17.5)
        self.assertAlmostEqual(profile.score(tolerance), 19.375)
        self.assertAlmostEqual(profile.score(overflow), 20.0)
        self.assertAlmostEqual(
            profile.marginal_score(overflow, EchoStat.ENERGY_REGEN, 12.4), 0
        )

    def test_required_er_uses_big_attack_weight_then_drops_to_small_attack(self):
        energy = EnergyRegenPolicy(
            required=120,
            comfort=125,
            overflow=130,
            tolerance_weight_ratio=5 / 9,
        )
        profile = CharacterScoringProfile.classified_main_dps(
            "type_1",
            weight_class=MainDpsWeightClass.TYPE_1,
            primary_damage_stat=EchoStat.HEAVY_ATTACK_DAMAGE,
            energy_policy=energy,
        )
        before_required = CharacterPanel.base(energy_regen=119)
        after_required = CharacterPanel.base(energy_regen=121)
        self.assertAlmostEqual(
            profile.marginal_score(before_required, EchoStat.ENERGY_REGEN, 1), 0.9
        )
        self.assertAlmostEqual(
            profile.marginal_score(after_required, EchoStat.ENERGY_REGEN, 1), 0.5
        )
        self.assertAlmostEqual(
            profile.weights[EchoStat.HEAVY_ATTACK_DAMAGE], 0.9
        )

        type_2 = CharacterScoringProfile.classified_main_dps(
            "type_2",
            weight_class=MainDpsWeightClass.TYPE_2,
            primary_damage_stat=EchoStat.HEAVY_ATTACK_DAMAGE,
            energy_policy=energy,
        )
        self.assertAlmostEqual(type_2.weights[EchoStat.HEAVY_ATTACK_DAMAGE], 0.5)


class TestCharacterBuild(unittest.TestCase):
    def test_slot_value_is_contextual_when_energy_regen_reaches_the_target(self):
        profile = CharacterScoringProfile.main_dps(energy_regen_target=130)
        build = CharacterBuild(
            profile=profile,
            base_panel=CharacterPanel.base(energy_regen=115),
            echoes=(
                EquippedEcho("slot_1", echo_with((EchoStat.ENERGY_REGEN, 8.4))),
                EquippedEcho("slot_2", echo_with((EchoStat.ENERGY_REGEN, 12.4))),
            ),
        )

        # The full set is already capped.  Removing either Echo exposes only
        # the part needed to return to 130, not its whole printed ER value.
        self.assertAlmostEqual(build.contextual_slot_score("slot_1"), 1.3)
        self.assertAlmostEqual(build.contextual_slot_score("slot_2"), 3.3)

    def test_replacement_delta_uses_the_full_set_context(self):
        profile = CharacterScoringProfile.main_dps(energy_regen_target=100)
        current = echo_with(
            (EchoStat.CRIT_RATE, 6.3),
            (EchoStat.RESONANCE_LIBERATION_DAMAGE, 11.6),
        )
        replacement = echo_with((EchoStat.CRIT_DAMAGE, 13.8))
        build = CharacterBuild(
            profile,
            CharacterPanel.base(),
            (EquippedEcho("target", current),),
        )

        expected = 13.8 * 0.9 - (6.3 * 1.8 + 11.6 * 0.5)
        self.assertAlmostEqual(build.replacement_delta("target", replacement), expected)


class TestContinuedImprovementCost(unittest.TestCase):
    def test_probability_and_resources_for_a_single_useful_stat(self):
        crit_only = CharacterScoringProfile(
            "crit_only",
            {EchoStat.CRIT_RATE: 1},
            energy_regen_target=100,
        )
        build = CharacterBuild(
            crit_only,
            CharacterPanel.base(),
            (EquippedEcho("target", echo_with((EchoStat.HP_FLAT, 320))),),
        )

        report = estimate_continued_improvement(build, trials=20_000, seed=151)
        estimate = report.estimates[0]

        # A five-line Echo contains a particular one of 13 types with
        # probability 5/13, regardless of that type's roll tier.
        self.assertAlmostEqual(estimate.success_probability, 5 / 13, delta=0.01)
        self.assertAlmostEqual(estimate.expected_attempts, 13 / 5, delta=0.08)
        self.assertGreater(estimate.expected_resources.tuners, 100)
        self.assertLess(estimate.expected_resources.tuners, 112)
        self.assertEqual(report.cheapest_slot, estimate)
        self.assertEqual(report.most_resource_efficient_slot, estimate)

    def test_unbeatable_slot_reports_infinite_continuation_cost(self):
        crit_only = CharacterScoringProfile(
            "crit_only",
            {EchoStat.CRIT_RATE: 1},
            energy_regen_target=100,
        )
        build = CharacterBuild(
            crit_only,
            CharacterPanel.base(),
            (EquippedEcho("perfect", echo_with((EchoStat.CRIT_RATE, 10.5))),),
        )

        estimate = estimate_continued_improvement(
            build, trials=1_000, seed=151
        ).estimates[0]
        self.assertEqual(estimate.success_probability, 0)
        self.assertTrue(math.isinf(estimate.expected_attempts))
        self.assertTrue(math.isinf(estimate.expected_resources.tuners))


if __name__ == "__main__":
    unittest.main()
