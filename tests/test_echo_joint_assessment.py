import unittest

from scripts.evaluate_xuanling_campaign import joint_best, joint_gain_samples
from src.echo.optimizer import EchoIdentity, NamedEchoBuild, NamedEquippedEcho
from src.echo.panel import CharacterPanel, CharacterScoringProfile, EnergyRegenPolicy
from src.echo.simulation import EchoState, EchoRoll, EchoStat, EchoUpgradeSimulator


def rolled(*pairs):
    return EchoState(tuple(EchoRoll.from_value(stat, value) for stat, value in pairs))


class JointAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.a = EchoIdentity('a', 3)
        self.b = EchoIdentity('b', 1)
        self.c = EchoIdentity('c', 1)
        profile = CharacterScoringProfile('test', {EchoStat.CRIT_RATE: 1.8, EchoStat.ENERGY_REGEN: .5},
            energy_policy=EnergyRegenPolicy(120, 135, 145, tolerance_weight_ratio=.5))
        self.build = NamedEchoBuild(profile, CharacterPanel.base(energy_regen=110), (
            NamedEquippedEcho('3C', self.a, rolled((EchoStat.ENERGY_REGEN, 10.8))),
            NamedEquippedEcho('1C', self.b, rolled((EchoStat.HP_FLAT, 320)))))
        self.items = list(self.build.echoes) + [NamedEquippedEcho('alternate', self.a,
            rolled((EchoStat.CRIT_RATE, 10.5)))]

    def test_joint_upgrade_unlocks_other_slot(self):
        candidate = rolled((EchoStat.ENERGY_REGEN, 10.8))
        single = self.build.best_replacement(self.b, candidate)
        gains = joint_gain_samples(self.build, self.items, self.b, (candidate,))
        self.assertAlmostEqual(gains[0], 18.9)
        self.assertGreater(gains[0], single.delta)

    def test_vectorized_envelope_matches_brute_force(self):
        simulator = EchoUpgradeSimulator(seed=15)
        samples = tuple(simulator.upgrade_to(EchoState(), 5).final for _ in range(40))
        for identity in (self.a, self.b, self.c):
            gains = joint_gain_samples(self.build, self.items, identity, samples)
            for sample, gain in zip(samples, gains):
                brute = joint_best(self.build, self.items + [NamedEquippedEcho('candidate', identity, sample)])
                self.assertAlmostEqual(gain, brute.score-self.build.score)

    def test_rejects_duplicate_name_complement(self):
        # Same name in different compatibility groups may appear in inventory,
        # but may not occupy both slots in a legal build.
        identity = EchoIdentity('a', 1)
        sample = rolled((EchoStat.CRIT_RATE, 10.5), (EchoStat.ENERGY_REGEN, 10.8))
        self.assertEqual(joint_gain_samples(self.build, self.items, identity, (sample,))[0], 0)
