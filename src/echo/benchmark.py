"""Paired fixed-attempt benchmarks for Echo upgrade policies.

The benchmark deliberately separates candidate availability from policy
decisions.  Every policy sees the same ordered stream of fully predetermined
Echo rolls, while only the number of revealed lines (and therefore resources
spent) depends on its decisions.
"""

from __future__ import annotations

from collections import Counter, OrderedDict
from dataclasses import dataclass
import hashlib
import math
from typing import Iterable, Protocol

from src.echo.optimizer import (
    EchoIdentity,
    NamedEchoBuild,
    NamedEquippedEcho,
    ReplacementOption,
)
from src.echo.simulation import (
    EchoRoll,
    EchoState,
    EchoStat,
    EchoUpgradeSimulator,
    ResourceAmount,
    ResourceCostModel,
)


DOUBLE_CRIT = frozenset({EchoStat.CRIT_RATE, EchoStat.CRIT_DAMAGE})


@dataclass(frozen=True)
class CandidateOutcome:
    """One eligible embryo and its five pre-rolled substats."""

    identity: EchoIdentity
    final_state: EchoState


@dataclass
class BenchmarkLedger:
    """Unlimited inventory ledger that still records gross and net costs."""

    gross: ResourceAmount = ResourceAmount()
    refunds: ResourceAmount = ResourceAmount()

    @property
    def net(self) -> ResourceAmount:
        return self.gross - self.refunds

    def spend(self, amount: ResourceAmount) -> None:
        self.gross = self.gross + amount

    def refund(self, amount: ResourceAmount) -> None:
        self.refunds = self.refunds + amount


@dataclass(frozen=True)
class PolicyDecision:
    next_slots: int | None
    reason: str
    details: dict[str, float | str]


@dataclass(frozen=True)
class BenchmarkRun:
    policy_name: str
    initial_build: NamedEchoBuild
    final_build: NamedEchoBuild
    attempts: int
    ledger: BenchmarkLedger
    score_curve: tuple[float, ...]
    total_score_curve: tuple[float, ...]
    net_tuners_curve: tuple[float, ...]
    net_exp_curve: tuple[float, ...]
    equipped_count_curve: tuple[int, ...]
    energy_curve: tuple[float, ...]
    energy_feasible_curve: tuple[bool, ...]
    kept: int
    discarded: int
    replacements: int
    reveals_by_stage: tuple[tuple[int, int], ...]
    discards_by_stage: tuple[tuple[int, int], ...]
    decision_reasons: tuple[tuple[str, int], ...]
    first_complete_attempt: int | None
    first_energy_feasible_attempt: int | None

    @property
    def final_echo_score(self) -> float:
        return self.score_curve[-1] if self.score_curve else 0.0

    @property
    def final_total_score(self) -> float:
        return self.total_score_curve[-1] if self.total_score_curve else self.initial_build.score

    @property
    def complete(self) -> bool:
        return all(echo.state.slot_count == 5 for echo in self.final_build.echoes)

    @property
    def energy_feasible(self) -> bool:
        return self.final_build.profile.is_feasible(self.final_build.panel)


class FixedAttemptPolicy(Protocol):
    name: str

    def decision(self, build: NamedEchoBuild, identity: EchoIdentity,
                 state: EchoState) -> PolicyDecision:
        ...

    def replacement(self, build: NamedEchoBuild, identity: EchoIdentity,
                    state: EchoState) -> ReplacementOption | None:
        ...


def empty_build(build: NamedEchoBuild) -> NamedEchoBuild:
    """Preserve the five slot groups but remove all equipped Echo substats."""

    echoes = tuple(
        NamedEquippedEcho(
            echo.slot,
            EchoIdentity(
                f"__empty__/{echo.slot}",
                echo.identity.cost,
                echo.identity.compatibility,
            ),
            EchoState(),
        )
        for echo in build.echoes
    )
    return NamedEchoBuild(build.profile, build.base_panel, echoes)


def candidate_stream(candidate_pool: Iterable[EchoIdentity], *, attempts: int,
                     seed: int) -> tuple[CandidateOutcome, ...]:
    """Create a shared 43311 candidate stream.

    Attempts cycle through one 4C, two 3C, and two 1C opportunities.  The 3C
    names alternate across their two public types; 1C names rotate across all
    configured types.  This keeps attempt allocation proportional to slots
    without pretending that the number of available names changes drop rate.
    """

    if attempts <= 0:
        raise ValueError("attempts must be positive")
    pool = tuple(candidate_pool)
    by_cost = {
        cost: tuple(identity for identity in pool if identity.cost == cost)
        for cost in (4, 3, 1)
    }
    if any(not identities for identities in by_cost.values()):
        raise ValueError("candidate pool must contain 4C, 3C, and 1C identities")

    simulator = EchoUpgradeSimulator(seed=seed)
    outcomes: list[CandidateOutcome] = []
    one_index = 0
    for index in range(attempts):
        position = index % 5
        if position == 0:
            identities = by_cost[4]
            identity = identities[(index // 5) % len(identities)]
        elif position in (1, 2):
            identities = by_cost[3]
            identity = identities[(index // 5 * 2 + position - 1) % len(identities)]
        else:
            identities = by_cost[1]
            identity = identities[one_index % len(identities)]
            one_index += 1
        outcomes.append(CandidateOutcome(
            identity,
            simulator.upgrade_to(EchoState(), 5).final,
        ))
    return tuple(outcomes)


def _empty_slots(build: NamedEchoBuild) -> tuple[str, ...]:
    return tuple(echo.slot for echo in build.echoes if echo.state.slot_count == 0)


def model_replacement(build: NamedEchoBuild, identity: EchoIdentity,
                      candidate: EchoState, *, minimum_gain: float = 0.0,
                      require_double_crit: bool = True) -> ReplacementOption | None:
    """Best contextual replacement, with bootstrap handling for empty sets."""

    if require_double_crit and not DOUBLE_CRIT.issubset(candidate.stats):
        return None
    options = build.replacement_options(identity, candidate)
    if not options:
        return None

    empty = set(_empty_slots(build))
    empty_options = [option for option in options if option.slot in empty]
    if empty_options:
        return max(empty_options, key=lambda option: option.delta)

    # While another COST/name is still missing, improving an already occupied
    # same-name slot remains legal, but global ER feasibility is deferred until
    # all five slots are present.
    if empty:
        eligible = [option for option in options if option.delta > minimum_gain + 1e-12]
        return max(eligible, key=lambda option: option.delta) if eligible else None

    current_er = build.panel.value(EchoStat.ENERGY_REGEN)
    if not build.profile.is_feasible(build.panel):
        eligible = []
        for option in options:
            candidate_er = build.panel_without(option.slot).add_echo(candidate).value(
                EchoStat.ENERGY_REGEN
            )
            if candidate_er > current_er + 1e-12 and option.delta > minimum_gain + 1e-12:
                eligible.append(option)
    else:
        eligible = [
            option for option in options
            if option.energy_feasible and option.delta > minimum_gain + 1e-12
        ]
    return max(eligible, key=lambda option: option.delta) if eligible else None


@dataclass(frozen=True)
class OpportunityMetrics:
    success_probability: float
    expected_gain: float
    expected_net_tuners: float
    expected_net_exp: float
    normalized_cost: float
    efficiency: float


class OpportunityCostPolicy:
    """Current rollout/opportunity-cost model adapted to unlimited resources."""

    name = "current_model"

    def __init__(self, *, rollout_samples: int = 400,
                 continuation_efficiency_ratio: float = 0.90,
                 analysis_seed: int = 15_100,
                 cost_model: ResourceCostModel | None = None) -> None:
        if rollout_samples <= 0:
            raise ValueError("rollout_samples must be positive")
        if not 0 < continuation_efficiency_ratio <= 1:
            raise ValueError("continuation_efficiency_ratio must be in (0, 1]")
        self.rollout_samples = rollout_samples
        self.continuation_efficiency_ratio = continuation_efficiency_ratio
        self.analysis_seed = analysis_seed
        self.cost_model = cost_model or ResourceCostModel()
        self._completion_cache: OrderedDict[tuple, tuple[EchoState, ...]] = OrderedDict()
        self._fresh_cache: OrderedDict[tuple, OpportunityMetrics] = OrderedDict()

    @staticmethod
    def _state_key(state: EchoState) -> tuple[tuple[str, int], ...]:
        return tuple((roll.stat.value, roll.tier) for roll in state.rolls)

    def _completions(self, state: EchoState) -> tuple[EchoState, ...]:
        key = (self._state_key(state), self.rollout_samples)
        cached = self._completion_cache.get(key)
        if cached is not None:
            self._completion_cache.move_to_end(key)
            return cached
        material = f"{self.analysis_seed}:{key!r}".encode()
        seed = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
        simulator = EchoUpgradeSimulator(seed=seed, cost_model=self.cost_model)
        completions = tuple(
            simulator.upgrade_to(state, 5).final
            for _ in range(self.rollout_samples)
        )
        self._completion_cache[key] = completions
        while len(self._completion_cache) > 256:
            self._completion_cache.popitem(last=False)
        return completions

    def evaluate(self, build: NamedEchoBuild, identity: EchoIdentity,
                 state: EchoState) -> OpportunityMetrics:
        successes = 0
        gain_sum = 0.0
        for final in self._completions(state):
            option = model_replacement(build, identity, final)
            if option is not None:
                successes += 1
                gain_sum += option.delta
        probability = successes / self.rollout_samples
        expected_gain = gain_sum / self.rollout_samples

        additional = self.cost_model.upgrade_cost(state.slot_count, 5)
        refund_now = self.cost_model.recycle_refund(state.slot_count)
        refund_full = self.cost_model.recycle_refund(5)
        expected_tuners = (
            additional.tuners + refund_now.tuners
            - (1 - probability) * refund_full.tuners
        )
        expected_exp = (
            additional.echo_exp + refund_now.echo_exp
            - (1 - probability) * refund_full.echo_exp
        )
        # With unlimited inventory there is no changing resource pressure.
        # Normalize each resource by one full +25 investment and use the
        # bottleneck fraction, preserving the original two-resource objective.
        full = self.cost_model.invested_cost(5, include_echo=False)
        normalized_cost = max(
            expected_tuners / full.tuners,
            expected_exp / full.echo_exp,
            0.0,
        )
        efficiency = expected_gain / normalized_cost if normalized_cost > 0 else 0.0
        return OpportunityMetrics(
            probability, expected_gain, expected_tuners, expected_exp,
            normalized_cost, efficiency,
        )

    def _fresh(self, build: NamedEchoBuild,
               identity: EchoIdentity) -> OpportunityMetrics:
        key = (build.fingerprint(), identity)
        cached = self._fresh_cache.get(key)
        if cached is not None:
            self._fresh_cache.move_to_end(key)
            return cached
        metrics = self.evaluate(build, identity, EchoState())
        self._fresh_cache[key] = metrics
        while len(self._fresh_cache) > 128:
            self._fresh_cache.popitem(last=False)
        return metrics

    def decision(self, build: NamedEchoBuild, identity: EchoIdentity,
                 state: EchoState) -> PolicyDecision:
        if state.slot_count >= 5:
            return PolicyDecision(None, "full", {})
        missing = DOUBLE_CRIT - state.stats
        if len(missing) > 5 - state.slot_count:
            return PolicyDecision(None, "double_crit_impossible", {
                "missing_required": float(len(missing)),
            })
        current = self.evaluate(build, identity, state)
        baseline = self._fresh(build, identity)
        threshold = baseline.efficiency * self.continuation_efficiency_ratio
        if current.success_probability <= 0 or current.expected_gain <= 0:
            return PolicyDecision(None, "no_sampled_upgrade", {
                "success_probability": current.success_probability,
            })
        if current.efficiency + 1e-12 < threshold:
            return PolicyDecision(None, "below_opportunity_cost", {
                "efficiency": current.efficiency,
                "threshold": threshold,
            })
        return PolicyDecision(state.slot_count + 1, "opportunity_continue", {
            "success_probability": current.success_probability,
            "efficiency": current.efficiency,
            "threshold": threshold,
        })

    def replacement(self, build: NamedEchoBuild, identity: EchoIdentity,
                    state: EchoState) -> ReplacementOption | None:
        return model_replacement(build, identity, state)


class FirstCritCommitPolicy:
    """Commit a high first Crit roll directly to +25, otherwise delegate.

    ``equivalent_threshold`` uses the same normalized Crit scale as scoring:
    Crit Rate is multiplied by two, while Crit Damage is used as displayed.
    Thus 9.9% Crit Rate and 19.8% Crit Damage share a threshold of 19.8.
    """

    def __init__(self, delegate: FixedAttemptPolicy, *,
                 equivalent_threshold: float) -> None:
        if not math.isfinite(equivalent_threshold) or equivalent_threshold < 0:
            raise ValueError("equivalent_threshold must be finite and non-negative")
        self.delegate = delegate
        self.equivalent_threshold = equivalent_threshold
        self.name = f"first_crit_commit_{equivalent_threshold:g}"

    def decision(self, build: NamedEchoBuild, identity: EchoIdentity,
                 state: EchoState) -> PolicyDecision:
        if state.slot_count == 1 and state.rolls[0].stat in DOUBLE_CRIT:
            roll = state.rolls[0]
            equivalent = roll.value * (2 if roll.stat == EchoStat.CRIT_RATE else 1)
            if equivalent + 1e-12 >= self.equivalent_threshold:
                return PolicyDecision(5, "first_crit_commit", {
                    "stat": roll.stat.value,
                    "value": roll.value,
                    "equivalent_value": equivalent,
                    "threshold": self.equivalent_threshold,
                })
        return self.delegate.decision(build, identity, state)

    def replacement(self, build: NamedEchoBuild, identity: EchoIdentity,
                    state: EchoState) -> ReplacementOption | None:
        return self.delegate.replacement(build, identity, state)


class PublishedHeuristicPolicy:
    """A/B/C/D staged rules supplied with the benchmark request."""

    name = "published_heuristic"
    ALWAYS = -math.inf

    def __init__(self, *, primary_damage: EchoStat = EchoStat.HEAVY_ATTACK_DAMAGE,
                 cost_model: ResourceCostModel | None = None) -> None:
        self.primary_damage = primary_damage
        self.cost_model = cost_model or ResourceCostModel()

    def _energy_effective(self, build: NamedEchoBuild) -> bool:
        policy = build.profile.energy_policy
        if policy is None:
            return build.profile.weights[EchoStat.ENERGY_REGEN] > 0
        return build.panel.value(EchoStat.ENERGY_REGEN) < policy.overflow - 1e-12

    def effective_stats(self, build: NamedEchoBuild) -> frozenset[EchoStat]:
        stats = {
            EchoStat.CRIT_RATE,
            EchoStat.CRIT_DAMAGE,
            EchoStat.ATTACK_PERCENT,
            EchoStat.ATTACK_FLAT,
            self.primary_damage,
        }
        if self._energy_effective(build):
            stats.add(EchoStat.ENERGY_REGEN)
        return frozenset(stats)

    def effective_rolls(self, build: NamedEchoBuild,
                        state: EchoState) -> tuple[EchoRoll, ...]:
        effective = self.effective_stats(build)
        return tuple(roll for roll in state.rolls if roll.stat in effective)

    @staticmethod
    def heuristic_score(state: EchoState, primary_damage: EchoStat) -> float:
        weights = {
            EchoStat.CRIT_RATE: 2.0,
            EchoStat.CRIT_DAMAGE: 1.0,
            EchoStat.ATTACK_PERCENT: 1.1,
            EchoStat.ATTACK_FLAT: 0.1,
            primary_damage: 0.7,
        }
        return sum(weights.get(roll.stat, 0.0) * roll.value for roll in state.rolls)

    @staticmethod
    def _single_effective_b2(roll: EchoRoll) -> bool:
        thresholds = {
            EchoStat.ATTACK_PERCENT: 11.6,
            EchoStat.CRIT_RATE: 9.3,
            EchoStat.CRIT_DAMAGE: 18.6,
        }
        threshold = thresholds.get(roll.stat)
        return threshold is not None and roll.value + 1e-12 >= threshold

    @staticmethod
    def _double_crit_equivalent(state: EchoState) -> bool:
        values = {roll.stat: roll.value for roll in state.rolls}
        if not DOUBLE_CRIT.issubset(values):
            return False
        return 2 * values[EchoStat.CRIT_RATE] + values[EchoStat.CRIT_DAMAGE] >= 32.4 - 1e-12

    def _c3_threshold(self, second: EchoRoll, crit_stat: EchoStat) -> float | None:
        value = second.value
        rows: dict[EchoStat, tuple[tuple[float, float, float], ...]] = {
            EchoStat.ATTACK_PERCENT: (
                (11.6, self.ALWAYS, self.ALWAYS),
                (10.9, self.ALWAYS, self.ALWAYS),
                (10.1, 6.9, 13.8),
                (9.4, 6.9, 13.8),
                (8.6, 7.5, 15.0),
                (7.9, 8.1, 16.2),
                (7.1, 8.1, 16.2),
                (6.4, 8.7, 17.4),
            ),
            self.primary_damage: (
                (11.6, 8.1, 16.2),
                (10.9, 8.7, 17.4),
                (10.1, 8.7, 17.4),
                (9.4, 9.3, 18.6),
                (8.6, 9.3, 18.6),
                (7.9, 9.3, 18.6),
                (7.1, 9.9, 19.8),
            ),
            EchoStat.ATTACK_FLAT: (
                (60.0, 9.3, 18.6),
                (50.0, 9.9, 19.8),
            ),
        }
        for listed_value, crit_rate, crit_damage in rows.get(second.stat, ()):
            if abs(value - listed_value) <= 0.051:
                return crit_rate if crit_stat == EchoStat.CRIT_RATE else crit_damage
        return None

    def _c3_passes(self, effective: tuple[EchoRoll, ...]) -> bool:
        crit = tuple(roll for roll in effective if roll.stat in DOUBLE_CRIT)
        if len(effective) != 2 or len(crit) != 1:
            return False
        second = next(roll for roll in effective if roll.stat not in DOUBLE_CRIT)
        threshold = self._c3_threshold(second, crit[0].stat)
        return threshold is not None and crit[0].value + 1e-12 >= threshold

    def decision(self, build: NamedEchoBuild, identity: EchoIdentity,
                 state: EchoState) -> PolicyDecision:
        del identity
        effective = self.effective_rolls(build, state)
        if state.slot_count == 1:
            if effective:
                return PolicyDecision(3, "A_effective_at_5", {})
            return PolicyDecision(None, "A_no_effective_at_5", {})
        if state.slot_count == 3:
            if len(effective) >= 2:
                return PolicyDecision(4, "B1_two_effective", {})
            if len(effective) == 1 and self._single_effective_b2(effective[0]):
                return PolicyDecision(4, "B2_high_single_roll", {})
            return PolicyDecision(None, "B_reject", {})
        if state.slot_count == 4:
            crit_count = sum(roll.stat in DOUBLE_CRIT for roll in effective)
            if len(effective) >= 3 and crit_count >= 1:
                return PolicyDecision(5, "C1_three_effective_with_crit", {})
            if crit_count == 2 and self._double_crit_equivalent(state):
                return PolicyDecision(5, "C2_double_crit_threshold", {})
            if self._c3_passes(effective):
                return PolicyDecision(5, "C3_table_threshold", {})
            return PolicyDecision(None, "C_reject", {})
        if state.slot_count >= 5:
            return PolicyDecision(None, "full", {})
        raise ValueError(f"heuristic policy does not expect +{state.level}")

    def _passes_d(self, build: NamedEchoBuild, state: EchoState) -> bool:
        if not DOUBLE_CRIT.issubset(state.stats):
            return False
        effective_count = len(self.effective_rolls(build, state))
        return (
            effective_count >= 4
            or self.heuristic_score(state, self.primary_damage) >= 43.7 - 1e-12
        )

    def replacement(self, build: NamedEchoBuild, identity: EchoIdentity,
                    state: EchoState) -> ReplacementOption | None:
        if not self._passes_d(build, state):
            return None
        candidate_score = self.heuristic_score(state, self.primary_damage)
        options: list[ReplacementOption] = []
        for option in build.replacement_options(identity, state):
            equipped = next(echo for echo in build.echoes if echo.slot == option.slot)
            old_score = self.heuristic_score(equipped.state, self.primary_damage)
            if equipped.state.slot_count == 0 or candidate_score > old_score + 1e-12:
                options.append(option)
        if not options:
            return None
        # The published rule compares its own D score, not the model score.
        return max(
            options,
            key=lambda option: candidate_score - self.heuristic_score(
                next(echo.state for echo in build.echoes if echo.slot == option.slot),
                self.primary_damage,
            ),
        )


def run_fixed_attempt_benchmark(initial_build: NamedEchoBuild,
                                outcomes: Iterable[CandidateOutcome],
                                policy: FixedAttemptPolicy,
                                *, cost_model: ResourceCostModel | None = None) -> BenchmarkRun:
    """Run one policy on a finite shared stream with unlimited inventory."""

    costs = cost_model or ResourceCostModel()
    build = initial_build
    ledger = BenchmarkLedger()
    stage_reveals: Counter[int] = Counter()
    stage_discards: Counter[int] = Counter()
    reasons: Counter[str] = Counter()
    kept = discarded = replacements = 0
    first_complete: int | None = None
    first_feasible: int | None = None
    base_score = initial_build.profile.score(initial_build.base_panel)
    score_curve: list[float] = []
    total_curve: list[float] = []
    net_tuners_curve: list[float] = []
    net_exp_curve: list[float] = []
    equipped_count_curve: list[int] = []
    energy_curve: list[float] = []
    energy_feasible_curve: list[bool] = []

    outcome_tuple = tuple(outcomes)
    for attempt, outcome in enumerate(outcome_tuple, start=1):
        state = EchoState()
        next_slots = 1
        option: ReplacementOption | None = None
        while True:
            ledger.spend(costs.upgrade_cost(
                state.slot_count,
                next_slots,
                consume_echo=state.slot_count == 0,
            ))
            state = EchoState(outcome.final_state.rolls[:next_slots])
            stage_reveals[state.level] += 1
            if state.slot_count == 5:
                option = policy.replacement(build, outcome.identity, state)
                if option is None:
                    reasons["D_reject_or_no_upgrade"] += 1
                break
            decision = policy.decision(build, outcome.identity, state)
            reasons[decision.reason] += 1
            if decision.next_slots is None:
                stage_discards[state.level] += 1
                break
            if not state.slot_count < decision.next_slots <= 5:
                raise ValueError("policy returned an invalid next stage")
            next_slots = decision.next_slots

        if option is None:
            ledger.refund(costs.recycle_refund(state.slot_count))
            discarded += 1
        else:
            old = next(echo for echo in build.echoes if echo.slot == option.slot)
            build = build.replace(option.slot, outcome.identity, state)
            kept += 1
            if old.state.slot_count:
                replacements += 1

        if first_complete is None and not _empty_slots(build):
            first_complete = attempt
        if (
            first_feasible is None
            and not _empty_slots(build)
            and build.profile.is_feasible(build.panel)
        ):
            first_feasible = attempt
        total_curve.append(build.score)
        score_curve.append(build.score - base_score)
        net_tuners_curve.append(ledger.net.tuners)
        net_exp_curve.append(ledger.net.echo_exp)
        equipped_count_curve.append(sum(
            echo.state.slot_count == 5 for echo in build.echoes
        ))
        energy_curve.append(build.panel.value(EchoStat.ENERGY_REGEN))
        energy_feasible_curve.append(
            not _empty_slots(build) and build.profile.is_feasible(build.panel)
        )

    return BenchmarkRun(
        policy.name,
        initial_build,
        build,
        len(outcome_tuple),
        ledger,
        tuple(score_curve),
        tuple(total_curve),
        tuple(net_tuners_curve),
        tuple(net_exp_curve),
        tuple(equipped_count_curve),
        tuple(energy_curve),
        tuple(energy_feasible_curve),
        kept,
        discarded,
        replacements,
        tuple(sorted(stage_reveals.items())),
        tuple(sorted(stage_discards.items())),
        tuple(sorted(reasons.items())),
        first_complete,
        first_feasible,
    )
