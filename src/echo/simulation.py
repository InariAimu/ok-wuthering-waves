"""Echo substat probability and resource-consumption simulation.

This module intentionally contains no OCR, UI, or upgrade-policy logic.  It is
the deterministic foundation on which those layers can later be validated.

The roll values and integer probability weights come from the probability
table published by the game and transcribed at:
https://github.com/2-07665/ww-echo-policy-calculator/blob/main/crates/echo_policy/src/data.rs

Resource constants are transcribed from:
https://github.com/2-07665/ww-echo-policy-calculator/blob/main/crates/echo_policy/src/cost.rs
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
import math
import random
from typing import Iterable


MAX_SUBSTATS = 5


class EchoStat(str, Enum):
    """The 13 mutually exclusive Echo substat types."""

    CRIT_RATE = "crit_rate"
    CRIT_DAMAGE = "crit_damage"
    ATTACK_PERCENT = "attack_percent"
    DEFENSE_PERCENT = "defense_percent"
    HP_PERCENT = "hp_percent"
    ATTACK_FLAT = "attack_flat"
    DEFENSE_FLAT = "defense_flat"
    HP_FLAT = "hp_flat"
    ENERGY_REGEN = "energy_regen"
    BASIC_ATTACK_DAMAGE = "basic_attack_damage"
    HEAVY_ATTACK_DAMAGE = "heavy_attack_damage"
    RESONANCE_SKILL_DAMAGE = "resonance_skill_damage"
    RESONANCE_LIBERATION_DAMAGE = "resonance_liberation_damage"


ALL_STATS = tuple(EchoStat)


@dataclass(frozen=True)
class RollTier:
    """One possible value of a substat and its unnormalised integer weight."""

    value: float
    weight: int


def _percent_tiers(values: Iterable[int], weights: Iterable[int]) -> tuple[RollTier, ...]:
    """Convert public 0.1-percent units into percentage-point values."""

    return tuple(RollTier(value / 10, weight) for value, weight in zip(values, weights, strict=True))


def _flat_tiers(values: Iterable[int], weights: Iterable[int]) -> tuple[RollTier, ...]:
    return tuple(RollTier(float(value), weight) for value, weight in zip(values, weights, strict=True))


_CRIT_WEIGHTS = (70, 70, 70, 24, 24, 24, 9, 9)
_COMMON_WEIGHTS = (7, 8, 21, 25, 18, 15, 6, 3)

_COMMON_PERCENT_VALUES = (64, 71, 79, 86, 94, 101, 109, 116)


ROLL_TABLE: dict[EchoStat, tuple[RollTier, ...]] = {
    EchoStat.CRIT_RATE: _percent_tiers((63, 69, 75, 81, 87, 93, 99, 105), _CRIT_WEIGHTS),
    EchoStat.CRIT_DAMAGE: _percent_tiers((126, 138, 150, 162, 174, 186, 198, 210), _CRIT_WEIGHTS),
    EchoStat.ATTACK_PERCENT: _percent_tiers(_COMMON_PERCENT_VALUES, _COMMON_WEIGHTS),
    EchoStat.DEFENSE_PERCENT: _percent_tiers((81, 90, 100, 109, 118, 128, 138, 147), _COMMON_WEIGHTS),
    EchoStat.HP_PERCENT: _percent_tiers(_COMMON_PERCENT_VALUES, _COMMON_WEIGHTS),
    EchoStat.ATTACK_FLAT: _flat_tiers((30, 40, 50, 60), (7, 54, 39, 3)),
    EchoStat.DEFENSE_FLAT: _flat_tiers((40, 50, 60, 70), (15, 46, 33, 9)),
    EchoStat.HP_FLAT: _flat_tiers((320, 360, 390, 430, 470, 510, 540, 580), _COMMON_WEIGHTS),
    EchoStat.ENERGY_REGEN: _percent_tiers((68, 76, 84, 92, 100, 108, 116, 124), _COMMON_WEIGHTS),
    EchoStat.BASIC_ATTACK_DAMAGE: _percent_tiers(_COMMON_PERCENT_VALUES, _COMMON_WEIGHTS),
    EchoStat.HEAVY_ATTACK_DAMAGE: _percent_tiers(_COMMON_PERCENT_VALUES, _COMMON_WEIGHTS),
    EchoStat.RESONANCE_SKILL_DAMAGE: _percent_tiers(_COMMON_PERCENT_VALUES, _COMMON_WEIGHTS),
    EchoStat.RESONANCE_LIBERATION_DAMAGE: _percent_tiers(_COMMON_PERCENT_VALUES, _COMMON_WEIGHTS),
}


@dataclass(frozen=True)
class EchoRoll:
    """A rolled substat represented by type and public-table tier index."""

    stat: EchoStat
    tier: int

    def __post_init__(self) -> None:
        tiers = ROLL_TABLE[self.stat]
        if not 0 <= self.tier < len(tiers):
            raise ValueError(f"tier {self.tier} is invalid for {self.stat.value}")

    @property
    def value(self) -> float:
        return ROLL_TABLE[self.stat][self.tier].value

    @classmethod
    def from_value(cls, stat: EchoStat, value: float, tolerance: float = 0.051) -> EchoRoll:
        """Resolve an OCR/read value to the nearest published tier."""

        tiers = ROLL_TABLE[stat]
        tier = min(range(len(tiers)), key=lambda index: abs(tiers[index].value - value))
        if abs(tiers[tier].value - value) > tolerance:
            raise ValueError(f"{value} does not match a published tier for {stat.value}")
        return cls(stat, tier)


@dataclass(frozen=True)
class EchoState:
    """The currently revealed substats of one Echo."""

    rolls: tuple[EchoRoll, ...] = ()

    def __post_init__(self) -> None:
        if len(self.rolls) > MAX_SUBSTATS:
            raise ValueError(f"an Echo can have at most {MAX_SUBSTATS} substats")
        stats = tuple(roll.stat for roll in self.rolls)
        if len(stats) != len(set(stats)):
            raise ValueError("an Echo cannot roll the same substat twice")

    @property
    def slot_count(self) -> int:
        return len(self.rolls)

    @property
    def level(self) -> int:
        return self.slot_count * 5

    @property
    def stats(self) -> frozenset[EchoStat]:
        return frozenset(roll.stat for roll in self.rolls)

    def add(self, roll: EchoRoll) -> EchoState:
        return EchoState(self.rolls + (roll,))


@dataclass(frozen=True)
class NextRoll:
    """One exact next-roll outcome and its conditional probability."""

    roll: EchoRoll
    probability: Fraction


def roll_tier_distribution(stat: EchoStat) -> tuple[tuple[RollTier, Fraction], ...]:
    """Return the normalized public value distribution for one chosen stat."""

    tiers = ROLL_TABLE[stat]
    total_weight = sum(tier.weight for tier in tiers)
    return tuple((tier, Fraction(tier.weight, total_weight)) for tier in tiers)


def next_roll_distribution(state: EchoState) -> tuple[NextRoll, ...]:
    """Return the exact next-line distribution, conditional on revealed types.

    Stat types are uniform among the types not already present.  Values use the
    stat-specific public histogram, so the returned probabilities sum exactly
    to one rather than relying on floating-point normalisation.
    """

    if state.slot_count >= MAX_SUBSTATS:
        return ()

    remaining_stats = tuple(stat for stat in ALL_STATS if stat not in state.stats)
    outcomes: list[NextRoll] = []
    for stat in remaining_stats:
        for tier_index, (tier, tier_probability) in enumerate(roll_tier_distribution(stat)):
            probability = Fraction(1, len(remaining_stats)) * tier_probability
            outcomes.append(NextRoll(EchoRoll(stat, tier_index), probability))
    return tuple(outcomes)


@dataclass(frozen=True)
class ResourceAmount:
    """Resources consumed (positive) or returned (when explicitly subtracted)."""

    echoes: float = 0
    tuners: float = 0
    echo_exp: float = 0
    shell_credits: float = 0

    @property
    def premium_tubes(self) -> float:
        return self.echo_exp / 5000

    def __add__(self, other: ResourceAmount) -> ResourceAmount:
        if not isinstance(other, ResourceAmount):
            return NotImplemented
        return ResourceAmount(
            self.echoes + other.echoes,
            self.tuners + other.tuners,
            self.echo_exp + other.echo_exp,
            self.shell_credits + other.shell_credits,
        )

    def __sub__(self, other: ResourceAmount) -> ResourceAmount:
        if not isinstance(other, ResourceAmount):
            return NotImplemented
        return ResourceAmount(
            self.echoes - other.echoes,
            self.tuners - other.tuners,
            self.echo_exp - other.echo_exp,
            self.shell_credits - other.shell_credits,
        )

    def __mul__(self, multiplier: float) -> ResourceAmount:
        if not isinstance(multiplier, (int, float)):
            return NotImplemented
        return ResourceAmount(
            self.echoes * multiplier,
            self.tuners * multiplier,
            self.echo_exp * multiplier,
            self.shell_credits * multiplier,
        )

    def __rmul__(self, multiplier: float) -> ResourceAmount:
        return self * multiplier

    def __truediv__(self, divisor: float) -> ResourceAmount:
        if not isinstance(divisor, (int, float)):
            return NotImplemented
        if divisor == 0:
            raise ZeroDivisionError("cannot divide resources by zero")
        return self * (1 / divisor)

    def covers(self, cost: ResourceAmount, *, ignore_echoes: bool = False,
               ignore_shell_credits: bool = False) -> bool:
        """Whether an inventory represented by ``self`` can pay ``cost``."""

        checks = [self.tuners >= cost.tuners, self.echo_exp >= cost.echo_exp]
        if not ignore_echoes:
            checks.append(self.echoes >= cost.echoes)
        if not ignore_shell_credits:
            checks.append(self.shell_credits >= cost.shell_credits)
        return all(checks)


@dataclass(frozen=True)
class ResourceCostModel:
    """Gross upgrade costs and recoverable resources for one Echo.

    ``exp_refund_ratio`` defaults to the observed effective ratio used by the
    reference model.  The theoretical maximum is 75%; item rounding makes 66%
    a more conservative default.  Shell Credits and Echo embryos are never
    refunded.
    """

    exp_refund_ratio: float = 0.66
    tuner_refund_ratio: float = 0.30

    EXP_CUMULATIVE = (0, 4_400, 16_500, 39_600, 79_100, 142_600)
    TUNERS_PER_REVEAL = 10
    SHELL_CREDITS_PER_TUNE = 2_000
    SHELL_CREDITS_PER_EXP = 0.1

    def __post_init__(self) -> None:
        if not math.isfinite(self.exp_refund_ratio) or not 0 <= self.exp_refund_ratio <= 0.75:
            raise ValueError("exp_refund_ratio must be between 0 and 0.75")
        if not math.isfinite(self.tuner_refund_ratio) or not 0 <= self.tuner_refund_ratio <= 1:
            raise ValueError("tuner_refund_ratio must be between 0 and 1")

    @staticmethod
    def _validate_slots(slot_count: int) -> None:
        if not isinstance(slot_count, int) or not 0 <= slot_count <= MAX_SUBSTATS:
            raise ValueError(f"slot_count must be an integer from 0 to {MAX_SUBSTATS}")

    def upgrade_cost(self, current_slots: int, target_slots: int, *,
                     consume_echo: bool = False) -> ResourceAmount:
        """Gross resources needed to reveal through ``target_slots``."""

        self._validate_slots(current_slots)
        self._validate_slots(target_slots)
        if target_slots < current_slots:
            raise ValueError("target_slots cannot be below current_slots")

        reveals = target_slots - current_slots
        echo_exp = self.EXP_CUMULATIVE[target_slots] - self.EXP_CUMULATIVE[current_slots]
        return ResourceAmount(
            echoes=1 if consume_echo else 0,
            tuners=reveals * self.TUNERS_PER_REVEAL,
            echo_exp=echo_exp,
            shell_credits=(
                reveals * self.SHELL_CREDITS_PER_TUNE
                + echo_exp * self.SHELL_CREDITS_PER_EXP
            ),
        )

    def invested_cost(self, slot_count: int, *, include_echo: bool = True) -> ResourceAmount:
        """All resources invested in an Echo from level 0 to this state."""

        return self.upgrade_cost(0, slot_count, consume_echo=include_echo)

    def recycle_refund(self, slot_count: int) -> ResourceAmount:
        """Resources returned when an Echo at this state is fed/recycled."""

        self._validate_slots(slot_count)
        return ResourceAmount(
            tuners=slot_count * self.TUNERS_PER_REVEAL * self.tuner_refund_ratio,
            echo_exp=self.EXP_CUMULATIVE[slot_count] * self.exp_refund_ratio,
        )

    def attempt_cost(self, target_slots: int, *, keep: bool) -> ResourceAmount:
        """Net cost of a fresh Echo upgraded to a stage, then kept or discarded."""

        invested = self.invested_cost(target_slots)
        if keep:
            return invested
        return invested - self.recycle_refund(target_slots)


@dataclass(frozen=True)
class UpgradeSimulation:
    initial: EchoState
    final: EchoState
    gross_cost: ResourceAmount

    @property
    def revealed(self) -> tuple[EchoRoll, ...]:
        return self.final.rolls[self.initial.slot_count:]


class EchoUpgradeSimulator:
    """Seedable, stateful Monte Carlo sampler backed by the exact roll table."""

    def __init__(self, seed: int | None = None,
                 cost_model: ResourceCostModel | None = None) -> None:
        self.rng = random.Random(seed)
        self.cost_model = cost_model or ResourceCostModel()

    def reveal_next(self, state: EchoState) -> EchoRoll:
        if state.slot_count >= MAX_SUBSTATS:
            raise ValueError("all five substats have already been revealed")

        remaining_stats = tuple(stat for stat in ALL_STATS if stat not in state.stats)
        stat = self.rng.choice(remaining_stats)
        tiers = ROLL_TABLE[stat]
        tier_index = self.rng.choices(
            range(len(tiers)),
            weights=(tier.weight for tier in tiers),
            k=1,
        )[0]
        return EchoRoll(stat, tier_index)

    def upgrade_to(self, state: EchoState, target_slots: int, *,
                   consume_echo: bool = False) -> UpgradeSimulation:
        ResourceCostModel._validate_slots(target_slots)
        if target_slots < state.slot_count:
            raise ValueError("target_slots cannot be below the current state")

        final = state
        while final.slot_count < target_slots:
            final = final.add(self.reveal_next(final))

        return UpgradeSimulation(
            initial=state,
            final=final,
            gross_cost=self.cost_model.upgrade_cost(
                state.slot_count,
                target_slots,
                consume_echo=consume_echo,
            ),
        )
