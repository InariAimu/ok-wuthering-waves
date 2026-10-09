import unittest

from src.echo import (
    CharacterPanel,
    CharacterScoringProfile,
    EchoIdentity,
    EchoRoll,
    EchoState,
    EchoStat,
    NamedEchoBuild,
    NamedEquippedEcho,
    MainDpsWeightClass,
    PolicyConfig,
    StagedUpgradeCampaign,
)
from src.echo.scenarios import xuanling_scenario


def echo_with(*rolls: tuple[EchoStat, float]) -> EchoState:
    return EchoState(tuple(EchoRoll.from_value(stat, value) for stat, value in rolls))


class TestInterchangeableSlots(unittest.TestCase):
    def setUp(self):
        self.profile = CharacterScoringProfile(
            "crit_only", {EchoStat.CRIT_RATE: 1}
        )
        self.identity_a = EchoIdentity("one_a", 1, "atk_main")
        self.identity_b = EchoIdentity("one_b", 1, "atk_main")
        self.identity_c = EchoIdentity("one_c", 1, "atk_main")
        self.build = NamedEchoBuild(
            self.profile,
            CharacterPanel.base(),
            (
                NamedEquippedEcho(
                    "one_1", self.identity_a,
                    echo_with((EchoStat.CRIT_RATE, 6.3)),
                ),
                NamedEquippedEcho(
                    "one_2", self.identity_b,
                    echo_with((EchoStat.HP_FLAT, 320)),
                ),
            ),
        )

    def test_unused_name_can_replace_both_slots(self):
        self.assertEqual(self.build.compatible_slots(self.identity_c), ("one_1", "one_2"))
        self.assertEqual(self.build.compatible_slots(self.identity_a), ("one_1",))

    def test_candidate_selects_the_best_valid_slot_after_rolling(self):
        candidate = echo_with((EchoStat.CRIT_RATE, 6.9))
        option = self.build.best_replacement(self.identity_c, candidate)
        self.assertIsNotNone(option)
        self.assertEqual(option.slot, "one_2")
        self.assertAlmostEqual(option.delta, 6.9)

    def test_possible_target_is_candidate_name_specific(self):
        current_name = self.build.best_possible_replacement(self.identity_a)
        unused_name = self.build.best_possible_replacement(self.identity_c)

        self.assertIsNotNone(current_name)
        self.assertIsNotNone(unused_name)
        self.assertEqual(current_name.slot, "one_1")
        self.assertEqual(unused_name.slot, "one_2")
        self.assertGreater(unused_name.final_score, current_name.final_score)

    def test_required_double_crit_rejects_candidates_missing_either_stat(self):
        required = frozenset({EchoStat.CRIT_RATE, EchoStat.CRIT_DAMAGE})
        crit_only = echo_with((EchoStat.CRIT_RATE, 10.5))
        double_crit = echo_with(
            (EchoStat.CRIT_RATE, 6.9),
            (EchoStat.CRIT_DAMAGE, 13.8),
        )
        self.assertIsNone(self.build.best_replacement(
            self.identity_c, crit_only, required_stats=required
        ))
        self.assertIsNotNone(self.build.best_replacement(
            self.identity_c, double_crit, required_stats=required
        ))

    def test_replacement_rotates_the_unused_name(self):
        candidate = echo_with((EchoStat.CRIT_RATE, 6.9))
        replaced = self.build.replace("one_2", self.identity_c, candidate)
        self.assertEqual(replaced.compatible_slots(self.identity_b), ("one_1", "one_2"))
        self.assertEqual(replaced.compatible_slots(self.identity_c), ("one_2",))


class TestStagedCampaign(unittest.TestCase):
    def test_campaign_records_decisions_and_never_overspends(self):
        profile = CharacterScoringProfile(
            "crit_only", {EchoStat.CRIT_RATE: 1}
        )
        old = EchoIdentity("old", 1)
        fresh = EchoIdentity("fresh", 1)
        build = NamedEchoBuild(
            profile,
            CharacterPanel.base(),
            (NamedEquippedEcho("one", old, echo_with((EchoStat.HP_FLAT, 320))),),
        )
        campaign = StagedUpgradeCampaign(
            build,
            (fresh,),
            tuners=100,
            echo_exp=200_000,
            config=PolicyConfig(
                rollout_samples=300,
                max_attempts=10,
                analysis_seed=1,
                outcome_seed=2,
            ),
        )
        result = campaign.run()

        self.assertGreater(result.attempts, 0)
        self.assertTrue(result.events)
        self.assertGreaterEqual(result.ledger.tuners, 0)
        self.assertGreaterEqual(result.ledger.echo_exp, 0)
        self.assertEqual(
            result.ledger.initial_tuners - result.ledger.net_tuners,
            result.ledger.tuners,
        )
        self.assertTrue({event.action for event in result.events} & {
            "discard_failure", "replace_success", "discard_resource_stop"
        })


class TestXuanlingScenario(unittest.TestCase):
    def test_transcribed_panel_and_interchangeable_groups(self):
        scenario = xuanling_scenario()
        build = scenario.build

        self.assertAlmostEqual(build.panel.value(EchoStat.ENERGY_REGEN), 137.6)
        self.assertTrue(build.profile.is_feasible(build.panel))
        self.assertEqual([echo.identity.cost for echo in build.echoes], [4, 3, 3, 1, 1])
        unused_one_cost = next(
            identity for identity in scenario.candidate_pool
            if identity.name.startswith("1C-C")
        )
        self.assertEqual(build.compatible_slots(unused_one_cost), ("1C-1", "1C-2"))

        contextual = {
            echo.slot: build.contextual_slot_score(echo.slot)
            for echo in build.echoes
        }
        self.assertEqual(min(contextual, key=contextual.get), "1C-2")

    def test_classified_scenario_uses_new_energy_thresholds(self):
        scenario = xuanling_scenario(
            required_er=120,
            comfort_er=125,
            overflow_er=130,
            weight_class=MainDpsWeightClass.TYPE_1,
        )
        profile = scenario.build.profile
        self.assertEqual(profile.energy_policy.required, 120)
        self.assertAlmostEqual(profile.weights[EchoStat.ENERGY_REGEN], 0.9)
        self.assertAlmostEqual(profile.weights[EchoStat.HEAVY_ATTACK_DAMAGE], 0.9)


if __name__ == "__main__":
    unittest.main()
