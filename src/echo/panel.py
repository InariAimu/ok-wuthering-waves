"""Configurable character-panel scoring and set-improvement estimation.

Damage-type shares depend on rotations, teammates, sequences, and weapons.
This module therefore implements an explicit scoring proxy rather than
claiming to be a universal damage simulator.  Character-specific profiles can
replace every default weight without changing the probability model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from fractions import Fraction
import math
from types import MappingProxyType
from typing import Mapping

from src.echo.simulation import (
    ALL_STATS,
    ROLL_TABLE,
    EchoState,
    EchoStat,
    EchoUpgradeSimulator,
    ResourceAmount,
    ResourceCostModel,
    roll_tier_distribution,
)


SPECIAL_DAMAGE_STATS = (
    EchoStat.BASIC_ATTACK_DAMAGE,
    EchoStat.HEAVY_ATTACK_DAMAGE,
    EchoStat.RESONANCE_SKILL_DAMAGE,
    EchoStat.RESONANCE_LIBERATION_DAMAGE,
)


@dataclass(frozen=True)
class EnergyRegenPolicy:
    """Hard ER requirement plus normal, tolerance, and overflow scoring bands."""

    required: float
    comfort: float
    overflow: float
    base: float = 100.0
    tolerance_weight_ratio: float = 1.0

    def __post_init__(self) -> None:
        values = (
            self.base, self.required, self.comfort, self.overflow,
            self.tolerance_weight_ratio,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("energy thresholds must be finite")
        if not self.base <= self.required <= self.comfort <= self.overflow:
            raise ValueError("energy thresholds must satisfy base <= required <= comfort <= overflow")
        if not 0 <= self.tolerance_weight_ratio <= 1:
            raise ValueError("tolerance_weight_ratio must be in [0, 1]")

    def is_feasible(self, energy_regen: float) -> bool:
        return energy_regen + 1e-12 >= self.required

    def marginal_weight_ratio(self, energy_regen: float) -> float:
        """Multiplier applied to the profile's ER weight at this panel value."""

        if energy_regen <= self.required:
            return 1.0
        if energy_regen <= self.comfort:
            return self.tolerance_weight_ratio
        if energy_regen >= self.overflow:
            return 0.0
        width = self.overflow - self.comfort
        if width == 0:
            return 0.0
        return self.tolerance_weight_ratio * (self.overflow - energy_regen) / width

    def utility(self, energy_regen: float, weight: float) -> float:
        """Continuous score obtained by integrating the marginal ER weight."""

        value = max(energy_regen, self.base)
        normal_end = min(value, self.required)
        score = weight * (normal_end - self.base)
        if value <= self.required:
            return score

        comfort_end = min(value, self.comfort)
        score += (
            weight * self.tolerance_weight_ratio
            * (comfort_end - self.required)
        )
        if value <= self.comfort or self.overflow == self.comfort:
            return score

        tolerance_width = self.overflow - self.comfort
        distance = min(value, self.overflow) - self.comfort
        return score + weight * self.tolerance_weight_ratio * (
            distance - distance * distance / (2 * tolerance_width)
        )


class MainDpsWeightClass(str, Enum):
    """Simplified common main-DPS substat priority templates."""

    TYPE_1 = "type1"
    TYPE_2 = "type2"


def _generic_main_dps_weights() -> dict[EchoStat, float]:
    """Weights enforcing crit = crit damage > ATK > generic damage bonus.

    Crit Damage rolls are twice the numeric size of Crit Rate rolls, hence the
    1.8:0.9 coefficients.  ATK is set to 0.9 so even its highest public roll is
    below the lowest Crit roll.  Generic damage bonuses use a conservative 0.5
    until a character profile supplies a more specific value.
    """

    weights = {stat: 0.0 for stat in ALL_STATS}
    weights.update({
        EchoStat.CRIT_RATE: 1.8,
        EchoStat.CRIT_DAMAGE: 0.9,
        EchoStat.ATTACK_PERCENT: 0.9,
        EchoStat.ATTACK_FLAT: 0.1,
        EchoStat.ENERGY_REGEN: 0.5,
    })
    weights.update({stat: 0.5 for stat in SPECIAL_DAMAGE_STATS})
    return weights


@dataclass(frozen=True)
class CharacterPanel:
    """Character values relevant to Echo substat scoring.

    Percentage values are stored as displayed percentage points (for example,
    6.3 Crit Rate), while flat ATK/HP/DEF use raw points.  ``values`` should
    contain stats from the character, weapon, teammates and every source that
    must remain when Echoes are replaced.
    """

    values: Mapping[EchoStat, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        copied: dict[EchoStat, float] = {}
        for stat, value in self.values.items():
            if not isinstance(stat, EchoStat):
                raise TypeError(f"panel key must be EchoStat, got {stat!r}")
            if not math.isfinite(value):
                raise ValueError(f"panel value for {stat.value} must be finite")
            copied[stat] = float(value)
        object.__setattr__(self, "values", MappingProxyType(copied))

    @classmethod
    def base(cls, *, energy_regen: float = 100.0,
             values: Mapping[EchoStat, float] | None = None) -> CharacterPanel:
        combined = dict(values or {})
        combined[EchoStat.ENERGY_REGEN] = energy_regen
        return cls(combined)

    def value(self, stat: EchoStat) -> float:
        return self.values.get(stat, 0.0)

    def add_values(self, additions: Mapping[EchoStat, float]) -> CharacterPanel:
        values = dict(self.values)
        for stat, value in additions.items():
            values[stat] = values.get(stat, 0.0) + value
        return CharacterPanel(values)

    def add_echo(self, echo: EchoState) -> CharacterPanel:
        values = dict(self.values)
        for roll in echo.rolls:
            values[roll.stat] = values.get(roll.stat, 0.0) + roll.value
        return CharacterPanel(values)


@dataclass(frozen=True)
class CharacterScoringProfile:
    """A character-specific proxy for comparing Echo substats."""

    name: str
    weights: Mapping[EchoStat, float]
    energy_regen_target: float | None = None
    energy_policy: EnergyRegenPolicy | None = None

    def __post_init__(self) -> None:
        complete = {stat: 0.0 for stat in ALL_STATS}
        for stat, weight in self.weights.items():
            if not isinstance(stat, EchoStat):
                raise TypeError(f"weight key must be EchoStat, got {stat!r}")
            if not math.isfinite(weight) or weight < 0:
                raise ValueError(f"weight for {stat.value} must be finite and non-negative")
            complete[stat] = float(weight)
        if self.energy_regen_target is not None:
            if not math.isfinite(self.energy_regen_target) or self.energy_regen_target < 0:
                raise ValueError("energy_regen_target must be finite and non-negative")
        if self.energy_policy is not None and self.energy_regen_target is not None:
            raise ValueError("use either energy_regen_target or energy_policy, not both")
        object.__setattr__(self, "weights", MappingProxyType(complete))

    @classmethod
    def main_dps(cls, name: str = "generic_main_dps", *,
                 energy_regen_target: float = 100.0,
                 overrides: Mapping[EchoStat, float] | None = None) -> CharacterScoringProfile:
        weights = _generic_main_dps_weights()
        weights.update(overrides or {})
        crit_floor = min(
            ROLL_TABLE[EchoStat.CRIT_RATE][0].value * weights[EchoStat.CRIT_RATE],
            ROLL_TABLE[EchoStat.CRIT_DAMAGE][0].value * weights[EchoStat.CRIT_DAMAGE],
        )
        attack_ceiling = max(
            ROLL_TABLE[EchoStat.ATTACK_PERCENT][-1].value * weights[EchoStat.ATTACK_PERCENT],
            ROLL_TABLE[EchoStat.ATTACK_FLAT][-1].value * weights[EchoStat.ATTACK_FLAT],
        )
        special_ceiling = max(
            ROLL_TABLE[stat][-1].value * weights[stat]
            for stat in SPECIAL_DAMAGE_STATS
        )
        if not crit_floor > attack_ceiling > special_ceiling:
            raise ValueError(
                "main DPS overrides must preserve Crit = Crit DMG > ATK > special damage "
                "across the published roll ranges; use CharacterScoringProfile directly "
                "for a profile outside this generic assumption"
            )
        return cls(name, weights, energy_regen_target)

    @classmethod
    def normalized_main_dps(
            cls, name: str, *, damage_coverage: Mapping[EchoStat, float],
            energy_policy: EnergyRegenPolicy,
            attack_weight: float = 0.9, flat_attack_weight: float = 0.1,
            energy_weight: float = 0.5) -> CharacterScoringProfile:
        """Build a profile where a damage bonus is ATK-equivalent at 100% coverage."""

        if not math.isfinite(attack_weight) or attack_weight <= 0:
            raise ValueError("attack_weight must be finite and positive")
        weights = {stat: 0.0 for stat in ALL_STATS}
        weights.update({
            EchoStat.CRIT_RATE: 1.8,
            EchoStat.CRIT_DAMAGE: 0.9,
            EchoStat.ATTACK_PERCENT: attack_weight,
            EchoStat.ATTACK_FLAT: flat_attack_weight,
            EchoStat.ENERGY_REGEN: energy_weight,
        })
        for stat, coverage in damage_coverage.items():
            if stat not in SPECIAL_DAMAGE_STATS:
                raise ValueError(f"{stat.value} is not a damage-bonus coverage stat")
            if not math.isfinite(coverage) or not 0 <= coverage <= 1:
                raise ValueError(f"coverage for {stat.value} must be in [0, 1]")
            weights[stat] = attack_weight * coverage
        return cls(name, weights, energy_policy=energy_policy)

    @classmethod
    def classified_main_dps(
            cls, name: str, *, weight_class: MainDpsWeightClass,
            primary_damage_stat: EchoStat,
            energy_policy: EnergyRegenPolicy,
            energy_is_required: bool = True) -> CharacterScoringProfile:
        """Build one of the two common priority templates described by users.

        Type 1: double Crit > ATK% = primary damage > flat ATK.
        Type 2: double Crit > ATK% > primary damage = flat ATK.
        Required ER uses the ATK% weight before the hard threshold; optional ER
        uses the flat-ATK-equivalent weight.
        """

        if primary_damage_stat not in SPECIAL_DAMAGE_STATS:
            raise ValueError("primary_damage_stat must be a damage-bonus stat")
        if not isinstance(weight_class, MainDpsWeightClass):
            weight_class = MainDpsWeightClass(weight_class)

        weights = {stat: 0.0 for stat in ALL_STATS}
        weights.update({
            EchoStat.CRIT_RATE: 1.8,
            EchoStat.CRIT_DAMAGE: 0.9,
            EchoStat.ATTACK_PERCENT: 0.9,
            EchoStat.ATTACK_FLAT: 0.1,
            EchoStat.ENERGY_REGEN: 0.9 if energy_is_required else 0.5,
            primary_damage_stat: 0.9 if weight_class == MainDpsWeightClass.TYPE_1 else 0.5,
        })
        return cls(name, weights, energy_policy=energy_policy)

    def effective_value(self, panel: CharacterPanel, stat: EchoStat) -> float:
        value = panel.value(stat)
        if stat == EchoStat.ENERGY_REGEN and self.energy_regen_target is not None:
            return min(value, self.energy_regen_target)
        return value

    def score(self, panel: CharacterPanel) -> float:
        score = 0.0
        for stat in ALL_STATS:
            if stat == EchoStat.ENERGY_REGEN and self.energy_policy is not None:
                score += self.energy_policy.utility(
                    panel.value(stat), self.weights[stat]
                )
            else:
                score += self.weights[stat] * self.effective_value(panel, stat)
        return score

    def is_feasible(self, panel: CharacterPanel) -> bool:
        if self.energy_policy is None:
            return True
        return self.energy_policy.is_feasible(panel.value(EchoStat.ENERGY_REGEN))

    def marginal_score(self, panel: CharacterPanel, stat: EchoStat, value: float) -> float:
        """Contextual value of adding one stat roll to the current panel."""

        before = self.score(panel)
        after = self.score(panel.add_values({stat: value}))
        return after - before

    def marginal_roll_table(self, panel: CharacterPanel) -> Mapping[
            EchoStat, tuple[MarginalRoll, ...]]:
        """Build the state-dependent ``13 × tier`` value table."""

        table = {
            stat: tuple(
                MarginalRoll(
                    stat=stat,
                    value=tier.value,
                    probability=probability,
                    score=self.marginal_score(panel, stat, tier.value),
                )
                for tier, probability in roll_tier_distribution(stat)
            )
            for stat in ALL_STATS
        }
        return MappingProxyType(table)


@dataclass(frozen=True)
class MarginalRoll:
    """Probability and contextual score for one stat's public roll tier."""

    stat: EchoStat
    value: float
    probability: Fraction
    score: float


@dataclass(frozen=True)
class EquippedEcho:
    slot: str
    state: EchoState

    def __post_init__(self) -> None:
        if not self.slot:
            raise ValueError("slot must not be empty")


@dataclass(frozen=True)
class CharacterBuild:
    """A base panel, scoring profile, and the currently equipped Echo set."""

    profile: CharacterScoringProfile
    base_panel: CharacterPanel
    echoes: tuple[EquippedEcho, ...]

    def __post_init__(self) -> None:
        slots = tuple(echo.slot for echo in self.echoes)
        if len(slots) != len(set(slots)):
            raise ValueError("equipped Echo slot names must be unique")

    def panel(self, *, exclude_slot: str | None = None,
              replacement: EchoState | None = None) -> CharacterPanel:
        panel = self.base_panel
        found = exclude_slot is None
        for equipped in self.echoes:
            if equipped.slot == exclude_slot:
                found = True
                continue
            panel = panel.add_echo(equipped.state)
        if not found:
            raise KeyError(f"unknown Echo slot: {exclude_slot}")
        if replacement is not None:
            if exclude_slot is None:
                raise ValueError("replacement requires exclude_slot")
            panel = panel.add_echo(replacement)
        return panel

    @property
    def score(self) -> float:
        return self.profile.score(self.panel())

    def contextual_slot_score(self, slot: str) -> float:
        """Score lost when this slot is removed from the full set."""

        return self.score - self.profile.score(self.panel(exclude_slot=slot))

    def replacement_delta(self, slot: str, candidate: EchoState) -> float:
        candidate_panel = self.panel(exclude_slot=slot, replacement=candidate)
        return self.profile.score(candidate_panel) - self.score


@dataclass(frozen=True)
class ImprovementEstimate:
    """Monte Carlo estimate for obtaining one strict upgrade in a slot."""

    slot: str
    current_contextual_score: float
    samples: int
    successes: int
    success_probability: float
    probability_low: float
    probability_high: float
    expected_attempts: float
    expected_gain_per_attempt: float
    expected_gain_on_success: float
    expected_resources: ResourceAmount
    resources_per_score: ResourceAmount


@dataclass(frozen=True)
class SetImprovementReport:
    """Current set score and comparable continuation costs for every slot."""

    current_score: float
    estimates: tuple[ImprovementEstimate, ...]
    trials: int
    seed: int | None
    minimum_gain: float
    assumption: str = (
        "eligible fresh Echoes are upgraded directly to +25; failures are recycled; "
        "early-stop policy savings are not included"
    )

    @property
    def cheapest_slot(self) -> ImprovementEstimate | None:
        if not self.estimates:
            return None
        return min(self.estimates, key=lambda item: item.expected_resources.tuners)

    @property
    def most_resource_efficient_slot(self) -> ImprovementEstimate | None:
        if not self.estimates:
            return None
        return min(self.estimates, key=lambda item: item.resources_per_score.tuners)


def _wilson_interval(successes: int, samples: int, z: float = 1.96) -> tuple[float, float]:
    probability = successes / samples
    denominator = 1 + z * z / samples
    centre = (probability + z * z / (2 * samples)) / denominator
    margin = z * math.sqrt(
        probability * (1 - probability) / samples + z * z / (4 * samples * samples)
    ) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


def _infinite_resources() -> ResourceAmount:
    return ResourceAmount(math.inf, math.inf, math.inf, math.inf)


def estimate_continued_improvement(
        build: CharacterBuild, *, trials: int = 100_000, seed: int | None = 151,
        minimum_gain: float = 0.0,
        cost_model: ResourceCostModel | None = None) -> SetImprovementReport:
    """Estimate the cost of the next strict upgrade for every equipped slot.

    One candidate sample is compared against every slot so their estimates are
    paired and directly comparable.  The caller is responsible for supplying
    candidates with the correct set, COST, and main stat; this model covers the
    common five-substat distribution after that filtering.
    """

    if not isinstance(trials, int) or trials <= 0:
        raise ValueError("trials must be a positive integer")
    if not math.isfinite(minimum_gain) or minimum_gain < 0:
        raise ValueError("minimum_gain must be finite and non-negative")

    costs = cost_model or ResourceCostModel()
    simulator = EchoUpgradeSimulator(seed=seed, cost_model=costs)
    current_score = build.score
    without_panels = tuple(build.panel(exclude_slot=echo.slot) for echo in build.echoes)
    successes = [0] * len(build.echoes)
    positive_gain_sums = [0.0] * len(build.echoes)

    for _ in range(trials):
        candidate = simulator.upgrade_to(EchoState(), 5).final
        for index, panel_without in enumerate(without_panels):
            candidate_score = build.profile.score(panel_without.add_echo(candidate))
            delta = candidate_score - current_score
            if delta > minimum_gain + 1e-12:
                successes[index] += 1
                positive_gain_sums[index] += delta

    kept_cost = costs.attempt_cost(5, keep=True)
    discarded_cost = costs.attempt_cost(5, keep=False)
    estimates: list[ImprovementEstimate] = []
    for index, equipped in enumerate(build.echoes):
        success_count = successes[index]
        probability = success_count / trials
        probability_low, probability_high = _wilson_interval(success_count, trials)
        gain_per_attempt = positive_gain_sums[index] / trials

        if success_count:
            attempts = 1 / probability
            gain_on_success = positive_gain_sums[index] / success_count
            expected_resources = kept_cost + discarded_cost * (attempts - 1)
            resources_per_score = expected_resources / gain_on_success
        else:
            attempts = math.inf
            gain_on_success = 0.0
            expected_resources = _infinite_resources()
            resources_per_score = _infinite_resources()

        estimates.append(ImprovementEstimate(
            slot=equipped.slot,
            current_contextual_score=build.contextual_slot_score(equipped.slot),
            samples=trials,
            successes=success_count,
            success_probability=probability,
            probability_low=probability_low,
            probability_high=probability_high,
            expected_attempts=attempts,
            expected_gain_per_attempt=gain_per_attempt,
            expected_gain_on_success=gain_on_success,
            expected_resources=expected_resources,
            resources_per_score=resources_per_score,
        ))

    return SetImprovementReport(
        current_score=current_score,
        estimates=tuple(estimates),
        trials=trials,
        seed=seed,
        minimum_gain=minimum_gain,
    )
