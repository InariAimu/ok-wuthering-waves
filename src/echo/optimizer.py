"""Interchangeable-slot Echo planning and reproducible staged simulation."""

from __future__ import annotations

from collections import Counter, OrderedDict
from dataclasses import dataclass, field
import hashlib
from itertools import combinations
import math
from typing import Iterable

from src.echo.panel import CharacterPanel, CharacterScoringProfile
from src.echo.simulation import (
    ALL_STATS,
    ROLL_TABLE,
    EchoRoll,
    EchoState,
    EchoStat,
    EchoUpgradeSimulator,
    ResourceAmount,
    ResourceCostModel,
)


@dataclass(frozen=True)
class EchoIdentity:
    """An Echo name plus the COST/main-stat compatibility group it belongs to."""

    name: str
    cost: int
    compatibility: str = "default"

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Echo name must not be empty")
        if self.cost not in (1, 3, 4):
            raise ValueError("Echo COST must be 1, 3, or 4")
        if not self.compatibility:
            raise ValueError("compatibility must not be empty")


@dataclass(frozen=True)
class NamedEquippedEcho:
    slot: str
    identity: EchoIdentity
    state: EchoState

    def __post_init__(self) -> None:
        if not self.slot:
            raise ValueError("slot must not be empty")


@dataclass(frozen=True)
class ReplacementOption:
    slot: str
    delta: float
    final_score: float
    energy_feasible: bool


@dataclass(frozen=True)
class NamedEchoBuild:
    """A scored set whose same-COST slots may be replaced interchangeably."""

    profile: CharacterScoringProfile
    base_panel: CharacterPanel
    echoes: tuple[NamedEquippedEcho, ...]

    def __post_init__(self) -> None:
        slots = tuple(echo.slot for echo in self.echoes)
        names = tuple(echo.identity.name for echo in self.echoes)
        if len(slots) != len(set(slots)):
            raise ValueError("equipped slot names must be unique")
        if len(names) != len(set(names)):
            raise ValueError("the same Echo name cannot be equipped twice")

    @property
    def panel(self) -> CharacterPanel:
        panel = self.base_panel
        for echo in self.echoes:
            panel = panel.add_echo(echo.state)
        return panel

    @property
    def score(self) -> float:
        return self.profile.score(self.panel)

    def panel_without(self, slot: str) -> CharacterPanel:
        panel = self.base_panel
        found = False
        for echo in self.echoes:
            if echo.slot == slot:
                found = True
                continue
            panel = panel.add_echo(echo.state)
        if not found:
            raise KeyError(f"unknown slot: {slot}")
        return panel

    def contextual_slot_score(self, slot: str) -> float:
        return self.score - self.profile.score(self.panel_without(slot))

    def compatible_slots(self, identity: EchoIdentity) -> tuple[str, ...]:
        """Slots the candidate can replace without creating a duplicate name."""

        compatible: list[str] = []
        for target in self.echoes:
            if target.identity.cost != identity.cost:
                continue
            if target.identity.compatibility != identity.compatibility:
                continue
            remaining_names = {
                echo.identity.name for echo in self.echoes if echo.slot != target.slot
            }
            if identity.name not in remaining_names:
                compatible.append(target.slot)
        return tuple(compatible)

    def replacement_options(self, identity: EchoIdentity,
                            candidate: EchoState) -> tuple[ReplacementOption, ...]:
        options: list[ReplacementOption] = []
        current_score = self.score
        for slot in self.compatible_slots(identity):
            panel = self.panel_without(slot).add_echo(candidate)
            options.append(ReplacementOption(
                slot=slot,
                delta=self.profile.score(panel) - current_score,
                final_score=self.profile.score(panel),
                energy_feasible=self.profile.is_feasible(panel),
            ))
        return tuple(options)

    def best_replacement(self, identity: EchoIdentity, candidate: EchoState, *,
                         minimum_gain: float = 0.0,
                         required_stats: frozenset[EchoStat] = frozenset()) -> ReplacementOption | None:
        if not required_stats.issubset(candidate.stats):
            return None
        options = [
            option for option in self.replacement_options(identity, candidate)
            if option.energy_feasible and option.delta > minimum_gain + 1e-12
        ]
        if not options:
            return None
        return max(options, key=lambda option: option.delta)

    def best_possible_replacement(
            self, identity: EchoIdentity, *, minimum_gain: float = 0.0,
            required_stats: frozenset[EchoStat] = frozenset(),
    ) -> ReplacementOption | None:
        """Best legal slot for this Echo name at its exact roll ceiling.

        Name uniqueness can make two identities with the same COST/main stat
        target different equipped slots.  Enumerating the 13 choose 5 stat
        sets keeps this planning exact for the profile's nonlinear panel score
        and energy-feasibility rules.
        """

        if len(required_stats) > 5:
            return None
        best: ReplacementOption | None = None
        for stats in combinations(ALL_STATS, 5):
            if not required_stats.issubset(stats):
                continue
            candidate = EchoState(tuple(
                EchoRoll(stat, len(ROLL_TABLE[stat]) - 1)
                for stat in stats
            ))
            option = self.best_replacement(
                identity,
                candidate,
                minimum_gain=minimum_gain,
                required_stats=required_stats,
            )
            if option is not None and (
                best is None
                or (option.final_score, option.delta, option.slot)
                > (best.final_score, best.delta, best.slot)
            ):
                best = option
        return best

    def replace(self, slot: str, identity: EchoIdentity,
                candidate: EchoState) -> NamedEchoBuild:
        replaced = False
        echoes: list[NamedEquippedEcho] = []
        for echo in self.echoes:
            if echo.slot == slot:
                echoes.append(NamedEquippedEcho(slot, identity, candidate))
                replaced = True
            else:
                echoes.append(echo)
        if not replaced:
            raise KeyError(f"unknown slot: {slot}")
        return NamedEchoBuild(self.profile, self.base_panel, tuple(echoes))

    def fingerprint(self) -> tuple:
        return tuple(
            (
                echo.slot,
                echo.identity.name,
                echo.identity.cost,
                tuple((roll.stat.value, roll.tier) for roll in echo.state.rolls),
            )
            for echo in self.echoes
        )


@dataclass(frozen=True)
class PolicyConfig:
    rollout_samples: int = 2_000
    minimum_gain: float = 0.0
    continuation_efficiency_ratio: float = 0.90
    max_attempts: int = 2_000
    analysis_seed: int = 15_100
    outcome_seed: int = 151
    required_stats: frozenset[EchoStat] = frozenset()
    deferred_recycling: bool = False

    def __post_init__(self) -> None:
        if self.rollout_samples <= 0:
            raise ValueError("rollout_samples must be positive")
        if self.minimum_gain < 0 or not math.isfinite(self.minimum_gain):
            raise ValueError("minimum_gain must be finite and non-negative")
        if not 0 < self.continuation_efficiency_ratio <= 1:
            raise ValueError("continuation_efficiency_ratio must be in (0, 1]")
        if self.max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if not all(isinstance(stat, EchoStat) for stat in self.required_stats):
            raise TypeError("required_stats must contain EchoStat values")


@dataclass
class ResourceLedger:
    initial_tuners: float
    initial_exp: float
    tuners: float = field(init=False)
    echo_exp: float = field(init=False)
    gross_tuners: float = 0.0
    gross_exp: float = 0.0
    refunded_tuners: float = 0.0
    refunded_exp: float = 0.0

    def __post_init__(self) -> None:
        if self.initial_tuners < 0 or self.initial_exp < 0:
            raise ValueError("initial resources must be non-negative")
        self.tuners = float(self.initial_tuners)
        self.echo_exp = float(self.initial_exp)

    def can_afford(self, cost: ResourceAmount) -> bool:
        return self.tuners + 1e-12 >= cost.tuners and self.echo_exp + 1e-12 >= cost.echo_exp

    def spend(self, cost: ResourceAmount) -> None:
        if not self.can_afford(cost):
            raise ValueError("resource ledger cannot afford this upgrade")
        self.tuners -= cost.tuners
        self.echo_exp -= cost.echo_exp
        self.gross_tuners += cost.tuners
        self.gross_exp += cost.echo_exp

    def refund(self, amount: ResourceAmount) -> None:
        self.tuners += amount.tuners
        self.echo_exp += amount.echo_exp
        self.refunded_tuners += amount.tuners
        self.refunded_exp += amount.echo_exp

    @property
    def net_tuners(self) -> float:
        return self.gross_tuners - self.refunded_tuners

    @property
    def net_exp(self) -> float:
        return self.gross_exp - self.refunded_exp

    def snapshot(self) -> dict[str, float]:
        return {
            "tuners": self.tuners,
            "echo_exp": self.echo_exp,
            "premium_tubes": self.echo_exp / 5_000,
        }


@dataclass(frozen=True)
class CompletionMetrics:
    success_probability: float
    expected_gain: float
    expected_gain_on_success: float
    expected_relative_tuners: float
    expected_relative_exp: float
    resource_pressure: float
    efficiency: float
    target_counts: tuple[tuple[str, int], ...]
    samples: int


@dataclass(frozen=True)
class CampaignEvent:
    index: int
    attempt: int
    action: str
    candidate: str | None
    cost: int | None
    level: int | None
    reason: str
    resources_before: dict[str, float]
    resources_after: dict[str, float]
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CampaignResult:
    initial_build: NamedEchoBuild
    final_build: NamedEchoBuild
    ledger: ResourceLedger
    events: tuple[CampaignEvent, ...]
    attempts: int
    successes: int
    failures: int
    stop_reason: str
    config: PolicyConfig


@dataclass(frozen=True)
class _RawMetrics:
    successes: int
    positive_gain_sum: float
    target_counts: tuple[tuple[str, int], ...]


class StagedUpgradeCampaign:
    """Receding-horizon staged policy with a reproducible Monte Carlo lookahead.

    The policy compares a partial candidate's expected score gain per fraction
    of the currently limiting resource with the best fresh-candidate option.
    It is deliberately an inspectable approximation to the full two-resource
    MDP, whose exact five-slot/build state is prohibitively large.
    """

    def __init__(self, build: NamedEchoBuild,
                 candidate_pool: Iterable[EchoIdentity], *,
                 tuners: float, echo_exp: float,
                 config: PolicyConfig | None = None,
                 cost_model: ResourceCostModel | None = None) -> None:
        self.initial_build = build
        self.build = build
        self.candidate_pool = tuple(candidate_pool)
        if not self.candidate_pool:
            raise ValueError("candidate_pool must not be empty")
        self.config = config or PolicyConfig()
        self.cost_model = cost_model or ResourceCostModel()
        self.ledger = ResourceLedger(tuners, echo_exp)
        self.actual_simulator = EchoUpgradeSimulator(seed=self.config.outcome_seed)
        self._completion_cache: OrderedDict[tuple, tuple[EchoState, ...]] = OrderedDict()
        self._raw_cache: OrderedDict[tuple, _RawMetrics] = OrderedDict()
        self._events: list[CampaignEvent] = []
        self._event_index = 0

    def _record(self, attempt: int, action: str, identity: EchoIdentity | None,
                state: EchoState | None, reason: str, before: dict[str, float],
                details: dict | None = None) -> None:
        self._event_index += 1
        self._events.append(CampaignEvent(
            index=self._event_index,
            attempt=attempt,
            action=action,
            candidate=identity.name if identity else None,
            cost=identity.cost if identity else None,
            level=state.level if state else None,
            reason=reason,
            resources_before=before,
            resources_after=self.ledger.snapshot(),
            details=details or {},
        ))

    @staticmethod
    def _partial_key(state: EchoState) -> tuple:
        return tuple((roll.stat.value, roll.tier) for roll in state.rolls)

    def _sample_seed(self, state: EchoState) -> int:
        material = f"{self.config.analysis_seed}:{self._partial_key(state)!r}".encode()
        return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")

    def _completion_samples(self, state: EchoState) -> tuple[EchoState, ...]:
        key = (self._partial_key(state), self.config.rollout_samples)
        if key in self._completion_cache:
            samples = self._completion_cache.pop(key)
            self._completion_cache[key] = samples
            return samples

        simulator = EchoUpgradeSimulator(seed=self._sample_seed(state))
        samples = tuple(
            simulator.upgrade_to(state, 5).final
            for _ in range(self.config.rollout_samples)
        )
        self._completion_cache[key] = samples
        while len(self._completion_cache) > 8:
            self._completion_cache.popitem(last=False)
        return samples

    def _raw_metrics(self, identity: EchoIdentity, state: EchoState) -> _RawMetrics:
        key = (self.build.fingerprint(), identity, self._partial_key(state))
        if key in self._raw_cache:
            raw = self._raw_cache.pop(key)
            self._raw_cache[key] = raw
            return raw

        successes = 0
        gain_sum = 0.0
        targets: Counter[str] = Counter()
        for final in self._completion_samples(state):
            option = self.build.best_replacement(
                identity,
                final,
                minimum_gain=self.config.minimum_gain,
                required_stats=self.config.required_stats,
            )
            if option is not None:
                successes += 1
                gain_sum += option.delta
                targets[option.slot] += 1
        raw = _RawMetrics(successes, gain_sum, tuple(sorted(targets.items())))
        self._raw_cache[key] = raw
        while len(self._raw_cache) > 256:
            self._raw_cache.popitem(last=False)
        return raw

    def evaluate(self, identity: EchoIdentity, state: EchoState) -> CompletionMetrics:
        raw = self._raw_metrics(identity, state)
        samples = self.config.rollout_samples
        probability = raw.successes / samples
        expected_gain = raw.positive_gain_sum / samples
        gain_on_success = raw.positive_gain_sum / raw.successes if raw.successes else 0.0

        additional = self.cost_model.upgrade_cost(state.slot_count, 5)
        if self.config.deferred_recycling:
            # The live task only marks failed Echoes as discarded.  They remain
            # in the inventory until the user processes them after the run, so
            # their possible refund cannot finance another reveal in this run.
            expected_tuners = additional.tuners
            expected_exp = additional.echo_exp
        else:
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
        tuner_pressure = expected_tuners / self.ledger.tuners if self.ledger.tuners > 0 else math.inf
        exp_pressure = expected_exp / self.ledger.echo_exp if self.ledger.echo_exp > 0 else math.inf
        pressure = max(tuner_pressure, exp_pressure, 0.0)
        efficiency = expected_gain / pressure if pressure > 0 and math.isfinite(pressure) else 0.0
        return CompletionMetrics(
            success_probability=probability,
            expected_gain=expected_gain,
            expected_gain_on_success=gain_on_success,
            expected_relative_tuners=expected_tuners,
            expected_relative_exp=expected_exp,
            resource_pressure=pressure,
            efficiency=efficiency,
            target_counts=raw.target_counts,
            samples=samples,
        )

    def _fresh_options(self) -> tuple[tuple[EchoIdentity, CompletionMetrics], ...]:
        empty = EchoState()
        return tuple((identity, self.evaluate(identity, empty)) for identity in self.candidate_pool)

    def fresh_options(self) -> tuple[tuple[EchoIdentity, CompletionMetrics], ...]:
        """Public snapshot of all fresh-candidate opportunity metrics."""

        return self._fresh_options()

    def apply_replacement(self, identity: EchoIdentity, state: EchoState, *,
                          slot: str | None = None) -> ReplacementOption:
        """Apply a validated live/simulated replacement and refresh scoring."""

        best = self.build.best_replacement(
            identity,
            state,
            minimum_gain=self.config.minimum_gain,
            required_stats=self.config.required_stats,
        )
        if best is None:
            raise ValueError("candidate is not a valid replacement")
        if slot is not None and slot != best.slot:
            raise ValueError(
                f"slot {slot!r} is not the model-selected replacement {best.slot!r}"
            )
        self.build = self.build.replace(best.slot, identity, state)
        return best

    def discard_refund(self, slot_count: int) -> ResourceAmount:
        """Return the resources available immediately after a discard.

        Live ok-ww runs use deferred recycling, in which case marking an Echo
        as discarded does not put any resources back into the task ledger.
        """

        if self.config.deferred_recycling:
            return ResourceAmount()
        return self.cost_model.recycle_refund(slot_count)

    @staticmethod
    def _metric_details(metrics: CompletionMetrics) -> dict:
        return {
            "success_probability": metrics.success_probability,
            "expected_gain": metrics.expected_gain,
            "expected_gain_on_success": metrics.expected_gain_on_success,
            "expected_relative_tuners": metrics.expected_relative_tuners,
            "expected_relative_exp": metrics.expected_relative_exp,
            "resource_pressure": metrics.resource_pressure,
            "efficiency": metrics.efficiency,
            "target_counts": dict(metrics.target_counts),
            "rollout_samples": metrics.samples,
        }

    def run(self) -> CampaignResult:
        attempts = 0
        successes = 0
        failures = 0
        stop_reason = "maximum attempts reached"

        while attempts < self.config.max_attempts:
            first_cost = self.cost_model.upgrade_cost(0, 1)
            if not self.ledger.can_afford(first_cost):
                stop_reason = "resources cannot pay for the first +5 reveal"
                break

            fresh_options = self._fresh_options()
            identity, fresh_metrics = max(
                fresh_options,
                key=lambda item: (
                    item[1].efficiency,
                    item[1].expected_gain,
                    len(self.build.compatible_slots(item[0])),
                ),
            )
            if fresh_metrics.success_probability == 0 or fresh_metrics.expected_gain <= 0:
                stop_reason = (
                    "no sampled fresh candidate can produce a valid strict upgrade "
                    f"in {self.config.rollout_samples} rollouts"
                )
                break

            attempts += 1
            state = EchoState()
            before = self.ledger.snapshot()
            self._record(
                attempts,
                "start",
                identity,
                state,
                "selected the fresh candidate with the highest expected gain per limiting-resource fraction",
                before,
                {
                    "compatible_slots": self.build.compatible_slots(identity),
                    "fresh_metrics": self._metric_details(fresh_metrics),
                },
            )

            while True:
                if state.slot_count == 5:
                    option = self.build.best_replacement(
                        identity,
                        state,
                        minimum_gain=self.config.minimum_gain,
                        required_stats=self.config.required_stats,
                    )
                    before = self.ledger.snapshot()
                    if option is None:
                        all_options = self.build.replacement_options(identity, state)
                        refund = self.discard_refund(5)
                        if refund != ResourceAmount():
                            self.ledger.refund(refund)
                        failures += 1
                        self._record(
                            attempts,
                            "discard_failure",
                            identity,
                            state,
                            "full candidate has no energy-feasible replacement above the minimum gain",
                            before,
                            {
                                "rolls": [(roll.stat.value, roll.value) for roll in state.rolls],
                                "replacement_options": [
                                    {
                                        "slot": item.slot,
                                        "delta": item.delta,
                                        "energy_feasible": item.energy_feasible,
                                    }
                                    for item in all_options
                                ],
                                "refund_tuners": refund.tuners,
                                "refund_exp": refund.echo_exp,
                                "deferred_recycling": self.config.deferred_recycling,
                            },
                        )
                    else:
                        old_score = self.build.score
                        old_name = next(
                            echo.identity.name for echo in self.build.echoes
                            if echo.slot == option.slot
                        )
                        self.apply_replacement(identity, state, slot=option.slot)
                        successes += 1
                        self._record(
                            attempts,
                            "replace_success",
                            identity,
                            state,
                            "candidate is the best valid replacement among all compatible same-COST slots",
                            before,
                            {
                                "slot": option.slot,
                                "replaced_name": old_name,
                                "rolls": [(roll.stat.value, roll.value) for roll in state.rolls],
                                "score_before": old_score,
                                "score_after": self.build.score,
                                "score_gain": option.delta,
                                "old_echo_retained": True,
                            },
                        )
                    break

                fresh_options = self._fresh_options()
                _, baseline = max(
                    fresh_options,
                    key=lambda item: (item[1].efficiency, item[1].expected_gain),
                )
                current = self.evaluate(identity, state)
                threshold = baseline.efficiency * self.config.continuation_efficiency_ratio
                missing_required = self.config.required_stats - state.stats
                impossible_required = len(missing_required) > 5 - state.slot_count

                if state.slot_count > 0 and (
                        impossible_required
                        or current.success_probability == 0
                        or current.efficiency < threshold):
                    before = self.ledger.snapshot()
                    refund = self.discard_refund(state.slot_count)
                    if refund != ResourceAmount():
                        self.ledger.refund(refund)
                    failures += 1
                    if impossible_required:
                        reason = "remaining slots cannot satisfy all required substats"
                    elif current.success_probability == 0:
                        reason = "no sampled valid completion remains"
                    else:
                        reason = "conditioned completion efficiency fell below the fresh-candidate opportunity cost"
                    self._record(
                        attempts,
                        "discard_failure",
                        identity,
                        state,
                        reason,
                        before,
                        {
                            "rolls": [(roll.stat.value, roll.value) for roll in state.rolls],
                            "current_metrics": self._metric_details(current),
                            "fresh_baseline_efficiency": baseline.efficiency,
                            "continue_threshold": threshold,
                            "missing_required_stats": sorted(
                                stat.value for stat in missing_required
                            ),
                            "refund_tuners": refund.tuners,
                            "refund_exp": refund.echo_exp,
                            "deferred_recycling": self.config.deferred_recycling,
                        },
                    )
                    break

                next_cost = self.cost_model.upgrade_cost(
                    state.slot_count, state.slot_count + 1
                )
                if not self.ledger.can_afford(next_cost):
                    before = self.ledger.snapshot()
                    refund = self.discard_refund(state.slot_count)
                    if refund != ResourceAmount():
                        self.ledger.refund(refund)
                    failures += 1
                    stop_reason = "resources cannot pay for the next staged reveal"
                    self._record(
                        attempts,
                        "discard_resource_stop",
                        identity,
                        state,
                        stop_reason,
                        before,
                        {
                            "required_tuners": next_cost.tuners,
                            "required_exp": next_cost.echo_exp,
                            "refund_tuners": refund.tuners,
                            "refund_exp": refund.echo_exp,
                            "deferred_recycling": self.config.deferred_recycling,
                        },
                    )
                    return CampaignResult(
                        self.initial_build, self.build, self.ledger,
                        tuple(self._events), attempts, successes, failures,
                        stop_reason, self.config,
                    )

                before = self.ledger.snapshot()
                self.ledger.spend(next_cost)
                roll = self.actual_simulator.reveal_next(state)
                state = state.add(roll)
                self._record(
                    attempts,
                    "continue",
                    identity,
                    state,
                    "conditioned completion efficiency meets the rolling fresh-candidate benchmark",
                    before,
                    {
                        "revealed_stat": roll.stat.value,
                        "revealed_value": roll.value,
                        "spent_tuners": next_cost.tuners,
                        "spent_exp": next_cost.echo_exp,
                        "current_metrics_before_reveal": self._metric_details(current),
                        "fresh_baseline_efficiency": baseline.efficiency,
                        "continue_threshold": threshold,
                    },
                )

        return CampaignResult(
            self.initial_build, self.build, self.ledger, tuple(self._events),
            attempts, successes, failures, stop_reason, self.config,
        )
