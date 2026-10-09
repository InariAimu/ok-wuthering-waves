"""Stable integration API for ok-ww's live Echo enhancement task.

This module deliberately has no screen coordinates or automation calls. The
caller supplies resource and candidate observations, then executes the action
returned by :class:`EchoEnhancementSession`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import math
from typing import Callable, Mapping, Protocol, runtime_checkable

from src.echo.optimizer import (
    CompletionMetrics,
    EchoIdentity,
    NamedEchoBuild,
    PolicyConfig,
    ResourceLedger,
    StagedUpgradeCampaign,
)
from src.echo.simulation import EchoRoll, EchoState, EchoStat, ResourceAmount, ResourceCostModel


PREMIUM_TUBE_EXP = 5_000


class ResourceSource(str, Enum):
    OK_WW = "ok_ww"
    MANUAL = "manual"
    MANUAL_FALLBACK = "manual_fallback"


@dataclass(frozen=True)
class EchoResourceSnapshot:
    """Resources available to the current run, before deferred salvage."""

    tuners: float
    echo_exp: float
    source: ResourceSource = ResourceSource.MANUAL
    detail: str = ""

    def __post_init__(self) -> None:
        for name, value in (("tuners", self.tuners), ("echo_exp", self.echo_exp)):
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")

    @property
    def premium_tubes(self) -> float:
        return self.echo_exp / PREMIUM_TUBE_EXP

    @classmethod
    def manual(cls, *, tuners: float, echo_exp: float = 0,
               premium_tubes: float = 0,
               detail: str = "user input") -> EchoResourceSnapshot:
        if premium_tubes < 0:
            raise ValueError("premium_tubes must be non-negative")
        return cls(
            tuners=tuners,
            echo_exp=echo_exp + premium_tubes * PREMIUM_TUBE_EXP,
            source=ResourceSource.MANUAL,
            detail=detail,
        )


@runtime_checkable
class EchoResourceProvider(Protocol):
    """Adapter implemented by ok-ww when it can read the live inventory."""

    def get_echo_resources(self) -> EchoResourceSnapshot | ResourceAmount | Mapping[str, object]:
        ...


ResourceProviderLike = EchoResourceProvider | Callable[
    [], EchoResourceSnapshot | ResourceAmount | Mapping[str, object]
]


def _provider_snapshot(value: EchoResourceSnapshot | ResourceAmount |
                       Mapping[str, object]) -> EchoResourceSnapshot:
    if isinstance(value, EchoResourceSnapshot):
        return replace(value, source=ResourceSource.OK_WW)
    if isinstance(value, ResourceAmount):
        return EchoResourceSnapshot(
            value.tuners, value.echo_exp, ResourceSource.OK_WW, "ok-ww provider"
        )
    if isinstance(value, Mapping):
        if "tuners" not in value:
            raise ValueError("resource provider result is missing 'tuners'")
        if "echo_exp" not in value and "premium_tubes" not in value:
            raise ValueError(
                "resource provider result needs 'echo_exp' or 'premium_tubes'"
            )
        tubes = float(value.get("premium_tubes", 0))
        return EchoResourceSnapshot(
            tuners=float(value.get("tuners", 0)),
            echo_exp=float(value.get("echo_exp", 0)) + tubes * PREMIUM_TUBE_EXP,
            source=ResourceSource.OK_WW,
            detail=str(value.get("detail", "ok-ww provider")),
        )
    raise TypeError("resource provider returned an unsupported value")


def resolve_echo_resources(*, provider: ResourceProviderLike | None = None,
                           manual_tuners: float = 0,
                           manual_echo_exp: float = 0,
                           manual_premium_tubes: float = 0,
                           prefer_ok_ww: bool = True) -> EchoResourceSnapshot:
    """Read ok-ww resources when possible, otherwise use manual values."""

    if prefer_ok_ww and provider is not None:
        try:
            value = (
                provider.get_echo_resources()
                if isinstance(provider, EchoResourceProvider)
                else provider()
            )
            return _provider_snapshot(value)
        except Exception as error:  # provider is an integration boundary
            fallback = EchoResourceSnapshot.manual(
                tuners=manual_tuners,
                echo_exp=manual_echo_exp,
                premium_tubes=manual_premium_tubes,
                detail=f"ok-ww provider failed: {error}",
            )
            return replace(fallback, source=ResourceSource.MANUAL_FALLBACK)
    return EchoResourceSnapshot.manual(
        tuners=manual_tuners,
        echo_exp=manual_echo_exp,
        premium_tubes=manual_premium_tubes,
    )


class EnhancementAction(str, Enum):
    CONTINUE = "continue"
    MARK_DISCARD = "mark_discard"
    KEEP_AND_LOCK = "keep_and_lock"
    STOP_RESOURCES = "stop_resources"


@dataclass(frozen=True)
class EnhancementDecision:
    action: EnhancementAction
    reason: str
    current_slots: int
    target_slots: int
    required_cost: ResourceAmount = ResourceAmount()
    metrics: CompletionMetrics | None = None
    fresh_baseline_efficiency: float = 0.0
    continue_threshold: float = 0.0
    replacement_slot: str | None = None
    score_gain: float = 0.0


@dataclass(frozen=True)
class PendingDiscard:
    identity: EchoIdentity
    state: EchoState
    reason: str
    recoverable_later: ResourceAmount


@dataclass(frozen=True)
class SessionSnapshot:
    build_score: float
    tuners: float
    echo_exp: float
    gross_tuners: float
    gross_exp: float
    pending_discards: int
    recoverable_tuners_later: float
    recoverable_exp_later: float


class EchoEnhancementSession:
    """Stateful decision service for one live ok-ww enhancement run.

    Failed candidates are only recorded as pending discards. No refund is
    credited to the live ledger; the user may recycle them after the task.
    """

    def __init__(self, build: NamedEchoBuild,
                 candidate_pool: tuple[EchoIdentity, ...] | list[EchoIdentity], *,
                 resources: EchoResourceSnapshot,
                 config: PolicyConfig | None = None,
                 cost_model: ResourceCostModel | None = None) -> None:
        policy = config or PolicyConfig()
        if not policy.deferred_recycling:
            policy = replace(policy, deferred_recycling=True)
        self.config = policy
        self.cost_model = cost_model or ResourceCostModel()
        self.resources = resources
        self._campaign = StagedUpgradeCampaign(
            build,
            candidate_pool,
            tuners=resources.tuners,
            echo_exp=resources.echo_exp,
            config=policy,
            cost_model=self.cost_model,
        )
        self.pending_discards: list[PendingDiscard] = []

    @property
    def build(self) -> NamedEchoBuild:
        return self._campaign.build

    @property
    def ledger(self) -> ResourceLedger:
        return self._campaign.ledger

    @property
    def candidate_pool(self) -> tuple[EchoIdentity, ...]:
        return self._campaign.candidate_pool

    def _best_fresh_metrics(self) -> CompletionMetrics:
        return max(
            (metrics for _, metrics in self._campaign.fresh_options()),
            key=lambda metrics: (metrics.efficiency, metrics.expected_gain),
        )

    def advise(self, identity: EchoIdentity, state: EchoState) -> EnhancementDecision:
        """Choose the next one-slot action from the currently observed state."""

        if identity not in self.candidate_pool:
            raise ValueError(f"candidate {identity.name!r} is not in this session's pool")

        if state.slot_count == 5:
            option = self.build.best_replacement(
                identity,
                state,
                minimum_gain=self.config.minimum_gain,
                required_stats=self.config.required_stats,
            )
            if option is None:
                return EnhancementDecision(
                    EnhancementAction.MARK_DISCARD,
                    "full candidate is not a valid strict upgrade",
                    5,
                    5,
                )
            return EnhancementDecision(
                EnhancementAction.KEEP_AND_LOCK,
                "candidate is the best valid same-COST replacement",
                5,
                5,
                replacement_slot=option.slot,
                score_gain=option.delta,
            )

        missing_required = self.config.required_stats - state.stats
        if len(missing_required) > 5 - state.slot_count:
            return EnhancementDecision(
                EnhancementAction.MARK_DISCARD,
                "remaining slots cannot satisfy required substats",
                state.slot_count,
                state.slot_count,
            )

        current = self._campaign.evaluate(identity, state)
        baseline = self._best_fresh_metrics()
        threshold = baseline.efficiency * self.config.continuation_efficiency_ratio
        if state.slot_count > 0 and (
            current.success_probability == 0 or current.efficiency < threshold
        ):
            reason = (
                "no sampled valid completion remains"
                if current.success_probability == 0
                else "completion efficiency is below fresh-candidate opportunity cost"
            )
            return EnhancementDecision(
                EnhancementAction.MARK_DISCARD,
                reason,
                state.slot_count,
                state.slot_count,
                metrics=current,
                fresh_baseline_efficiency=baseline.efficiency,
                continue_threshold=threshold,
            )

        target_slots = state.slot_count + 1
        cost = self.cost_model.upgrade_cost(state.slot_count, target_slots)
        if not self.ledger.can_afford(cost):
            return EnhancementDecision(
                EnhancementAction.STOP_RESOURCES,
                "resources cannot pay for the next staged reveal",
                state.slot_count,
                target_slots,
                required_cost=cost,
                metrics=current,
                fresh_baseline_efficiency=baseline.efficiency,
                continue_threshold=threshold,
            )
        return EnhancementDecision(
            EnhancementAction.CONTINUE,
            "conditioned completion efficiency meets the fresh-candidate benchmark",
            state.slot_count,
            target_slots,
            required_cost=cost,
            metrics=current,
            fresh_baseline_efficiency=baseline.efficiency,
            continue_threshold=threshold,
        )

    def record_reveal(self, previous: EchoState, observed: EchoState) -> ResourceAmount:
        """Debit gross cost after the game reveals one or more new lines."""

        if observed.slot_count <= previous.slot_count:
            raise ValueError("observed state must contain at least one new substat")
        if observed.rolls[:previous.slot_count] != previous.rolls:
            raise ValueError("observed state does not extend the previous state")
        cost = self.cost_model.upgrade_cost(previous.slot_count, observed.slot_count)
        self.ledger.spend(cost)
        return cost

    def record_discard(self, identity: EchoIdentity, state: EchoState,
                       reason: str) -> PendingDiscard:
        """Record a discard marker without crediting a refund to this run."""

        pending = PendingDiscard(
            identity,
            state,
            reason,
            self.cost_model.recycle_refund(state.slot_count),
        )
        self.pending_discards.append(pending)
        return pending

    def record_keep(self, identity: EchoIdentity, state: EchoState,
                    slot: str | None = None) -> str:
        option = self._campaign.apply_replacement(identity, state, slot=slot)
        return option.slot

    def synchronize_resources(self, snapshot: EchoResourceSnapshot) -> None:
        """Replace remaining inventory after an explicit ok-ww re-read."""

        self.resources = snapshot
        self.ledger.tuners = float(snapshot.tuners)
        self.ledger.echo_exp = float(snapshot.echo_exp)
        self.ledger.initial_tuners = self.ledger.tuners + self.ledger.net_tuners
        self.ledger.initial_exp = self.ledger.echo_exp + self.ledger.net_exp

    def snapshot(self) -> SessionSnapshot:
        recoverable = sum(
            (item.recoverable_later for item in self.pending_discards),
            start=ResourceAmount(),
        )
        return SessionSnapshot(
            build_score=self.build.score,
            tuners=self.ledger.tuners,
            echo_exp=self.ledger.echo_exp,
            gross_tuners=self.ledger.gross_tuners,
            gross_exp=self.ledger.gross_exp,
            pending_discards=len(self.pending_discards),
            recoverable_tuners_later=recoverable.tuners,
            recoverable_exp_later=recoverable.echo_exp,
        )


_OCR_STAT_MATCHERS: tuple[tuple[tuple[str, ...], EchoStat], ...] = (
    (("暴击伤害", "暴擊傷害"), EchoStat.CRIT_DAMAGE),
    (("暴击", "暴擊"), EchoStat.CRIT_RATE),
    (("共鸣效率", "共鳴效率", "效率"), EchoStat.ENERGY_REGEN),
    (("普攻",), EchoStat.BASIC_ATTACK_DAMAGE),
    (("重击", "重擊"), EchoStat.HEAVY_ATTACK_DAMAGE),
    (("共鸣解放", "共鳴解放", "解放"), EchoStat.RESONANCE_LIBERATION_DAMAGE),
    (("共鸣技能", "共鳴技能", "技能"), EchoStat.RESONANCE_SKILL_DAMAGE),
)


def echo_state_from_ocr_pairs(pairs: list[tuple[str, str]] |
                              tuple[tuple[str, str], ...]) -> EchoState:
    """Convert paired simplified/traditional Chinese OCR text into model rolls."""

    rolls: list[EchoRoll] = []
    for property_text, value_text in pairs:
        normalized_value = value_text.replace("％", "%").strip()
        try:
            value = float(normalized_value.rstrip("% "))
        except ValueError as error:
            raise ValueError(f"cannot parse Echo value {value_text!r}") from error

        stat = next(
            (candidate for needles, candidate in _OCR_STAT_MATCHERS
             if any(needle in property_text for needle in needles)),
            None,
        )
        if stat is None:
            is_percent = "%" in normalized_value
            if "攻击" in property_text or "攻擊" in property_text:
                stat = EchoStat.ATTACK_PERCENT if is_percent else EchoStat.ATTACK_FLAT
            elif "生命" in property_text:
                stat = EchoStat.HP_PERCENT if is_percent else EchoStat.HP_FLAT
            elif "防御" in property_text or "防禦" in property_text:
                stat = EchoStat.DEFENSE_PERCENT if is_percent else EchoStat.DEFENSE_FLAT
            else:
                raise ValueError(f"unknown Echo substat {property_text!r}")
        rolls.append(EchoRoll.from_value(stat, value))
    return EchoState(tuple(rolls))
