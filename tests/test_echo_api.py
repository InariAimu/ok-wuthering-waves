import unittest

from src.echo import (
    CharacterPanel,
    CharacterScoringProfile,
    EchoEnhancementSession,
    EchoIdentity,
    EchoResourceSnapshot,
    EchoRoll,
    EchoState,
    EchoStat,
    EnhancementAction,
    NamedEchoBuild,
    NamedEquippedEcho,
    PolicyConfig,
    ResourceSource,
    echo_state_from_ocr_pairs,
    resolve_echo_resources,
)


def state_with(*rolls: tuple[EchoStat, float]) -> EchoState:
    return EchoState(tuple(EchoRoll.from_value(stat, value) for stat, value in rolls))


class TestResourceAdapter(unittest.TestCase):
    def test_okww_provider_takes_precedence_and_accepts_tubes(self):
        snapshot = resolve_echo_resources(
            provider=lambda: {"tuners": 6621, "premium_tubes": 1275},
            manual_tuners=1,
            manual_echo_exp=1,
        )

        self.assertEqual(snapshot.source, ResourceSource.OK_WW)
        self.assertEqual(snapshot.tuners, 6621)
        self.assertEqual(snapshot.echo_exp, 6_375_000)

    def test_provider_failure_falls_back_to_manual_values(self):
        def failed_provider():
            raise RuntimeError("OCR unavailable")

        snapshot = resolve_echo_resources(
            provider=failed_provider,
            manual_tuners=200,
            manual_premium_tubes=3,
            manual_echo_exp=500,
        )

        self.assertEqual(snapshot.source, ResourceSource.MANUAL_FALLBACK)
        self.assertEqual(snapshot.tuners, 200)
        self.assertEqual(snapshot.echo_exp, 15_500)
        self.assertIn("OCR unavailable", snapshot.detail)

    def test_incomplete_provider_result_does_not_silently_zero_resources(self):
        snapshot = resolve_echo_resources(
            provider=lambda: {"tuners": 100},
            manual_tuners=80,
            manual_echo_exp=40_000,
        )

        self.assertEqual(snapshot.source, ResourceSource.MANUAL_FALLBACK)
        self.assertEqual(snapshot.tuners, 80)
        self.assertEqual(snapshot.echo_exp, 40_000)


class TestOcrConversion(unittest.TestCase):
    def test_simplified_and_traditional_stats_map_to_public_tiers(self):
        state = echo_state_from_ocr_pairs((
            ("暴擊傷害", "21.0％"),
            ("攻击", "11.6%"),
            ("重击伤害加成", "7.1%"),
            ("攻击", "50"),
        ))

        self.assertEqual(
            state.stats,
            frozenset({
                EchoStat.CRIT_DAMAGE,
                EchoStat.ATTACK_PERCENT,
                EchoStat.HEAVY_ATTACK_DAMAGE,
                EchoStat.ATTACK_FLAT,
            }),
        )


class TestLiveEnhancementSession(unittest.TestCase):
    def setUp(self):
        profile = CharacterScoringProfile("crit", {EchoStat.CRIT_RATE: 1})
        self.old = EchoIdentity("old", 1)
        self.fresh = EchoIdentity("fresh", 1)
        self.build = NamedEchoBuild(
            profile,
            CharacterPanel.base(),
            (NamedEquippedEcho(
                "one",
                self.old,
                state_with((EchoStat.HP_FLAT, 320)),
            ),),
        )

    def session(self, tuners=100, echo_exp=200_000):
        return EchoEnhancementSession(
            self.build,
            (self.fresh,),
            resources=EchoResourceSnapshot.manual(
                tuners=tuners, echo_exp=echo_exp
            ),
            config=PolicyConfig(rollout_samples=100, analysis_seed=1),
        )

    def test_live_session_forces_deferred_recycling(self):
        session = self.session()
        self.assertTrue(session.config.deferred_recycling)
        self.assertEqual(session._campaign.discard_refund(5).tuners, 0)
        self.assertEqual(session._campaign.discard_refund(5).echo_exp, 0)
        decision = session.advise(self.fresh, EchoState())
        self.assertEqual(decision.metrics.expected_relative_tuners, 50)
        self.assertEqual(decision.metrics.expected_relative_exp, 142_600)

    def test_marking_discard_never_credits_live_resources(self):
        session = self.session()
        empty = EchoState()
        revealed = state_with((EchoStat.HP_PERCENT, 6.4))
        session.record_reveal(empty, revealed)
        before = session.snapshot()

        pending = session.record_discard(self.fresh, revealed, "not competitive")
        after = session.snapshot()

        self.assertEqual(after.tuners, before.tuners)
        self.assertEqual(after.echo_exp, before.echo_exp)
        self.assertEqual(after.pending_discards, 1)
        self.assertGreater(pending.recoverable_later.echo_exp, 0)
        self.assertEqual(after.recoverable_exp_later, pending.recoverable_later.echo_exp)

    def test_zero_resources_stops_without_marking_candidate(self):
        session = self.session(tuners=0, echo_exp=0)
        decision = session.advise(self.fresh, EchoState())

        self.assertEqual(decision.action, EnhancementAction.STOP_RESOURCES)
        self.assertEqual(session.snapshot().pending_discards, 0)


if __name__ == "__main__":
    unittest.main()
