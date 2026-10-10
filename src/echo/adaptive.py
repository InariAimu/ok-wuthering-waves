"""Joint-inventory live policy, inventory-aware scheduling and audit events.

No UI input or physical recycling occurs in this module. Logical equipment
and economic refunds are explicitly separate from the game's equipped set
and spendable inventory.
"""
from __future__ import annotations

from collections import Counter, OrderedDict
from dataclasses import asdict, dataclass
import math
from typing import Callable, Iterable, Mapping

import numpy as np

from src.echo.api import EchoEnhancementSession, EnhancementAction, EnhancementDecision
from src.echo.decision_log import DecisionJournal
from src.echo.joint import JointInventory, echo_key
from src.echo.optimizer import (
    CampaignEvent, CampaignResult, CompletionMetrics, EchoIdentity, NamedEquippedEcho,
    StagedUpgradeCampaign, _RawMetrics,
)
from src.echo.simulation import EchoState, EchoUpgradeSimulator, ResourceAmount
from src.echo.workflow import named_echo_build_to_dict, policy_config_to_dict


@dataclass(frozen=True)
class AdaptivePolicy:
    switch_efficiency_ratio: float = 1.25
    missing_candidate_stop_ratio: float = 0.50
    fresh_samples: int = 20_000
    maximum_samples: int = 80_000

    def __post_init__(self):
        if not math.isfinite(self.switch_efficiency_ratio) or self.switch_efficiency_ratio < 1:
            raise ValueError('switch_efficiency_ratio must be finite and >= 1')
        if not math.isfinite(self.missing_candidate_stop_ratio) or not 0 < self.missing_candidate_stop_ratio <= 1:
            raise ValueError('missing_candidate_stop_ratio must be in (0, 1]')
        if (not isinstance(self.fresh_samples, int) or self.fresh_samples < 2 or
                not isinstance(self.maximum_samples, int) or self.maximum_samples < self.fresh_samples):
            raise ValueError('sample counts must be integers with 2 <= fresh <= maximum')


@dataclass(frozen=True)
class CandidateRanking:
    identity: EchoIdentity
    remaining: int | None
    availability: str
    metrics: CompletionMetrics
    relative_efficiency: float
    gain_standard_error: float
    efficiency_interval: tuple[float, float]
    retained_probability: float | None = None


@dataclass(frozen=True)
class CandidateSelection:
    action: str
    reason: str
    selected: EchoIdentity | None
    best_legal: EchoIdentity | None
    rankings: tuple[CandidateRanking, ...]
    refined: bool = False


class _JointCampaign(StagedUpgradeCampaign):
    def __init__(self, *args, inventory: JointInventory, **kwargs):
        super().__init__(*args, **kwargs)
        self.inventory = inventory
        self.estimates = OrderedDict()
        self.samples_cache = OrderedDict()

    def estimate(self, identity, state, count):
        key = (self.inventory.revision, self.build.fingerprint(), identity, state.rolls, count)
        if key in self.estimates:
            self.estimates.move_to_end(key)
            return self.estimates[key]
        partial = state.rolls
        samples = self.samples_cache.get(partial, ())
        if len(samples) < count:
            simulator = EchoUpgradeSimulator(seed=self._sample_seed(state))
            samples = tuple(simulator.upgrade_to(state, 5).final for _ in range(count))
            self.samples_cache[partial] = samples
            self.samples_cache.move_to_end(partial)
            while len(self.samples_cache) > 4:
                self.samples_cache.popitem(last=False)
        gains, targets = self.inventory.gains(self.build, identity, samples[:count],
            required_stats=self.config.required_stats, minimum_gain=self.config.minimum_gain)
        successful = gains > 0
        counts = Counter(self.build.echoes[index].slot for index in targets[successful])
        raw = _RawMetrics(int(np.sum(successful)), float(np.sum(gains)), tuple(sorted(counts.items())))
        stderr = float(np.std(gains, ddof=1)/math.sqrt(count)) if count > 1 else 0.0
        retained = self.inventory.retained_samples(identity, samples[:count], required_stats=self.config.required_stats)
        self.estimates[key] = raw, stderr, float(np.mean(retained | successful))
        while len(self.estimates) > 256:
            self.estimates.popitem(last=False)
        return self.estimates[key]

    def _raw_metrics(self, identity, state):
        return self.estimate(identity, state, self.config.rollout_samples)[0]

    def evaluate(self, identity, state):
        raw, _, retained = self.estimate(identity, state, self.config.rollout_samples)
        return self._metrics_from_raw(raw, state, include_refunds=True, retained_probability=retained)


class AdaptiveEnhancementSession(EchoEnhancementSession):
    def __init__(self, build, candidate_pool, *, resources, config=None, cost_model=None,
                 completed_inventory: Iterable[NamedEquippedEcho] = (),
                 availability: Mapping[EchoIdentity, int | None] | None = None,
                 adaptive_policy: AdaptivePolicy | None = None,
                 journal: DecisionJournal | None = None,
                 event_listener: Callable | None = None, mode='live', source=''):
        completed_inventory = tuple(completed_inventory)
        groups = {(echo.identity.cost, echo.identity.compatibility) for echo in build.echoes}
        pool = tuple(dict.fromkeys((*candidate_pool, *(echo.identity for echo in build.echoes),
            *(echo.identity for echo in completed_inventory
              if (echo.identity.cost, echo.identity.compatibility) in groups))))
        super().__init__(build, pool, resources=resources, config=config, cost_model=cost_model)
        self.adaptive_policy = adaptive_policy or AdaptivePolicy()
        self.inventory = JointInventory(build, completed_inventory)
        self._campaign = _JointCampaign(build, pool, tuners=resources.tuners,
            echo_exp=resources.echo_exp, config=self.config, cost_model=self.cost_model,
            inventory=self.inventory)
        self._campaign.build = self.inventory.optimize(build)
        self.remaining = {identity: None for identity in pool}
        self.stock_evidence = {identity: 'not checked' for identity in pool}
        self.stock_status = {identity: 'unknown' for identity in pool}
        for identity, quantity in (availability or {}).items():
            self._set_stock(identity, quantity, 'supplied inventory snapshot')
        self.journal = journal
        self.event_listener = event_listener
        self.mode = mode
        self.source = source
        self.active_identity = None
        self.active_state = EchoState()
        self.candidate_number = 0
        self.completed_candidates = 0
        self.previous_identity = None
        self.stop_reason = ''
        self.unreconciled_upgrade = False
        self._full_preview = None
        self._last_decision = None
        self.events = []
        self.emit('session_start', dict(
            original_build=named_echo_build_to_dict(build),
            completed_inventory=[dict(identity=asdict(item.identity), state=asdict(item.state))
                                 for item in self.inventory.items.values()],
            acquisition_difficulty=dict(easiest_to_hardest=[4, 1, 3], quantitative_multiplier=None),
            assumptions=dict(metrics='joint full-completion Monte Carlo; staged decisions recede after each reveal',
                             refunds='expected economic cost only; no physical recycling',
                             exp_cost='minimum model EXP unless corrected by resource observation')))

    def audit_snapshot(self):
        resources = asdict(self.snapshot())
        resources.update(economic_net_tuners=resources['gross_tuners']-resources['recoverable_tuners_later'],
                         economic_net_exp=resources['gross_exp']-resources['recoverable_exp_later'])
        return dict(mode=self.mode, source=self.source, policy=policy_config_to_dict(self.config),
            adaptive_policy=asdict(self.adaptive_policy), cost_model=asdict(self.cost_model),
            economic_refunds_included=True, physical_refunds_credited=False,
            policy_id='joint-inventory-adaptive-v1',
            build=named_echo_build_to_dict(self.build), build_score=self.build.score,
            resources=resources, candidate_number=self.candidate_number,
            completed_candidates=self.completed_candidates, stop_reason=self.stop_reason,
            unreconciled_upgrade=self.unreconciled_upgrade,
            active_identity=asdict(self.active_identity) if self.active_identity else None,
            active_state=asdict(self.active_state),
            stock=[dict(identity=asdict(identity), remaining=self.remaining[identity],
                        availability=self.stock_status[identity], evidence=self.stock_evidence[identity])
                   for identity in self.candidate_pool])

    def emit(self, kind, payload):
        event = dict(kind=kind, payload=payload, candidate_number=self.candidate_number)
        self.events.append(event)
        if self.journal:
            self.journal.append(kind, payload, self.audit_snapshot())
        if self.event_listener:
            self.event_listener(kind, payload)

    def _set_stock(self, identity, count, evidence):
        if identity not in self.candidate_pool:
            raise ValueError('stock identity is not in the candidate pool')
        if count is not None and (not isinstance(count, int) or isinstance(count, bool) or count < 0):
            raise ValueError('stock count must be a non-negative integer or None')
        self.remaining[identity] = count
        self.stock_evidence[identity] = evidence
        self.stock_status[identity] = 'missing' if count == 0 else ('present' if count else 'unknown')

    def observe_stock(self, identity, count, *, evidence, confirmed_available=False):
        if identity not in self.candidate_pool:
            if not self.inventory.compatible(identity):
                return
            self._campaign.candidate_pool += (identity,)
        # A later unreadable quantity must not erase a verified count that
        # already accounts for this session's first reveals. A newly read
        # numeric count remains authoritative; zero/unknown are not carried
        # into a newly observed available stack.
        previous_count = self.remaining.get(identity)
        if (count is None and confirmed_available and previous_count is not None
                and previous_count > 0):
            count = previous_count
            evidence += '; retained verified quantity minus reconciled first reveals'
        self._set_stock(identity, count, evidence)
        if count is None and confirmed_available:
            self.stock_status[identity] = 'present'
        self.emit('stock_observation', dict(identity=asdict(identity), count=count, evidence=evidence,
                                           availability=self.stock_status[identity]))

    def rankings(self, samples=None):
        count = samples or self.adaptive_policy.fresh_samples
        rows = []
        for identity in self.candidate_pool:
            raw, stderr, retained = self._campaign.estimate(identity, EchoState(), count)
            metrics = self._campaign._metrics_from_raw(raw, EchoState(), include_refunds=True, samples=count,
                                                       retained_probability=retained)
            # This is a Monte Carlo mean interval, not a claim of exact rates.
            z2 = 1.96**2
            center = (retained+z2/(2*count))/(1+z2/count)
            radius = 1.96*math.sqrt(retained*(1-retained)/count+z2/(4*count*count))/(1+z2/count)
            low_pressure = self._campaign._metrics_from_raw(raw, EchoState(), include_refunds=True,
                samples=count, retained_probability=max(0, center-radius)).resource_pressure
            high_pressure = self._campaign._metrics_from_raw(raw, EchoState(), include_refunds=True,
                samples=count, retained_probability=min(1, center+radius)).resource_pressure
            interval = ((max(0, metrics.expected_gain-1.96*stderr)/high_pressure,
                         (metrics.expected_gain+1.96*stderr)/low_pressure)
                        if low_pressure > 0 and math.isfinite(high_pressure) else (0.0, 0.0))
            rows.append(CandidateRanking(identity, self.remaining[identity], self.stock_status[identity],
                                         metrics, 0, stderr, interval, retained))
        best = max((row.metrics.efficiency for row in rows), default=0)
        return tuple(sorted((CandidateRanking(row.identity, row.remaining, row.availability, row.metrics,
            row.metrics.efficiency/best if best else 0, row.gain_standard_error, row.efficiency_interval,
            row.retained_probability)
            for row in rows), key=lambda row: (row.metrics.efficiency, row.metrics.expected_gain), reverse=True))

    def _best_fresh_metrics(self):
        # Preserve the absent target's opportunity cost instead of silently
        # making weak leftovers the new benchmark.
        return self.rankings()[0].metrics

    def choose_candidate(self):
        if not self.ledger.can_afford(self.cost_model.upgrade_cost(0, 1)):
            result = CandidateSelection('stop_resources', '现有余额不足以支付下一件的首次开孔', None, None, ())
            self.stop_reason = result.reason
            self.emit('candidate_ranking', asdict(result))
            return result
        policy = self.adaptive_policy
        rows = self.rankings()
        refined = False
        for iteration in range(2):
            best = rows[0]
            available = next((row for row in rows if row.remaining != 0), None)
            previous = next((row for row in rows if row.identity == self.previous_identity and row.remaining != 0), None)
            boundary_uncertain = bool(available and best.remaining == 0 and
                (available.efficiency_interval[1]-available.efficiency_interval[0] > 1e-12 or
                 best.efficiency_interval[1]-best.efficiency_interval[0] > 1e-12) and
                available.efficiency_interval[0] <= policy.missing_candidate_stop_ratio * best.efficiency_interval[1] and
                available.efficiency_interval[1] >= policy.missing_candidate_stop_ratio * best.efficiency_interval[0])
            switch_uncertain = bool(previous and available and previous.identity != available.identity and
                available.efficiency_interval[0] <= policy.switch_efficiency_ratio * previous.efficiency_interval[1] and
                available.efficiency_interval[1] >= policy.switch_efficiency_ratio * previous.efficiency_interval[0])
            if (iteration == 0 and policy.maximum_samples > policy.fresh_samples and
                    (best.metrics.expected_gain == 0 or boundary_uncertain or switch_uncertain)):
                rows = self.rankings(policy.maximum_samples)
                refined = True
                continue
            break
        if best.metrics.expected_gain <= 0:
            action, reason, selected = 'stop_no_sampled_gain', '加密抽样仍未观察到有效联合提升，请检查目标或增加样本', None
        elif available is None:
            action, reason, selected = 'stop_missing_stock', '可用胚子已耗尽，请获取排名靠前的目标胚子', None
        elif best.remaining == 0 and boundary_uncertain:
            action, reason, selected = 'stop_uncertain', '加密抽样后仍跨越缺货停手阈值，请查阅置信区间再决定', None
        elif best.remaining == 0 and available.relative_efficiency < policy.missing_candidate_stop_ratio:
            action, reason, selected = 'stop_missing_stock', '剩余胚子效率低于缺货停手阈值，请获取更高收益的胚子', None
        else:
            selected = available.identity
            action, reason = 'select', '选择当前有库存或待确认库存中净材料效率最高的胚子'
            if (previous and previous.metrics.efficiency > 0 and
                    previous.relative_efficiency >= policy.missing_candidate_stop_ratio and
                    available.metrics.efficiency < policy.switch_efficiency_ratio * previous.metrics.efficiency):
                selected = previous.identity
                reason = '其他目标效率未达到切换倍率，继续当前目标'
        result = CandidateSelection(action, reason, selected, best.identity, rows, refined)
        if selected is None:
            self.stop_reason = reason
        self.emit('candidate_ranking', asdict(result))
        return result

    def begin_candidate(self, identity):
        if self.active_identity is not None:
            raise ValueError('previous candidate is unresolved')
        if identity not in self.candidate_pool or self.remaining[identity] == 0:
            raise ValueError('candidate is unavailable')
        self.active_identity = identity
        self.active_state = EchoState()
        self.candidate_number += 1
        self._full_preview = None
        self._last_decision = None
        self.emit('candidate_started', dict(identity=asdict(identity)))

    def advise(self, identity, state):
        if self.active_identity != identity or self.active_state != state:
            raise ValueError('decision must match the active observed candidate')
        if state.slot_count < 5:
            decision = super().advise(identity, state)
        else:
            key = echo_key(identity, state)
            if self._full_preview is None:
                duplicate = key in self.inventory.items
                allowed = self.config.required_stats.issubset(state.stats)
                before = self.build
                if allowed:
                    self.inventory.add(identity, state)
                best = self.inventory.optimize(before)
                if best.score <= before.score + self.config.minimum_gain + 1e-9:
                    best = before
                self._full_preview = (best, duplicate, allowed)
                self.emit('joint_optimization', dict(candidate=asdict(identity), state=asdict(state),
                    previous_build=named_echo_build_to_dict(before), proposed_build=named_echo_build_to_dict(best),
                    score_gain=best.score-before.score, inventory_revision=self.inventory.revision,
                    changed_slots=[old.slot for old, new in zip(before.echoes, best.echoes)
                                   if echo_key(old.identity, old.state) != echo_key(new.identity, new.state)],
                    equipment_applied_in_game=False))
            best, duplicate, allowed = self._full_preview
            used = next((echo.slot for echo in best.echoes if echo_key(echo.identity, echo.state) == key), None)
            keep = allowed and not duplicate and (used is not None or not self.inventory.is_dominated(identity, state))
            decision = EnhancementDecision(
                EnhancementAction.KEEP_AND_LOCK if keep else EnhancementAction.MARK_DISCARD,
                ('joint inventory upgrade' if best.score > self.build.score + 1e-9 else
                 'non-dominated reserve for future joint combinations') if keep else
                'duplicate, dominated, or required-substat-ineligible full candidate',
                5, 5, replacement_slot=used if keep else None,
                score_gain=best.score-self.build.score if keep else 0)
        self._last_decision = decision
        self.emit('enhancement_decision', dict(identity=asdict(identity), state=asdict(state), decision=asdict(decision)))
        return decision

    def record_reveal(self, previous, observed):
        if self.active_identity is None or previous != self.active_state:
            raise ValueError('reveal must extend the active candidate')
        cost = super().record_reveal(previous, observed)
        if previous.slot_count == 0 and self.remaining[self.active_identity] is not None:
            self.remaining[self.active_identity] -= 1
            if self.remaining[self.active_identity] == 0:
                self.stock_status[self.active_identity] = 'missing'
                self.stock_evidence[self.active_identity] = 'observed stack consumed by first reveal'
        self.active_state = observed
        self.emit('reveal', dict(identity=asdict(self.active_identity), previous=asdict(previous),
                                observed=asdict(observed), gross_cost=asdict(cost), cost_basis='minimum_upgrade_model'))
        return cost

    def _finish(self):
        self.previous_identity = self.active_identity
        self.active_identity = None
        self.active_state = EchoState()
        self.completed_candidates += 1
        self._full_preview = None

    def record_keep(self, identity, state, slot=None):
        if self.active_identity != identity or self.active_state != state or self._full_preview is None:
            raise ValueError('keep requires the active full-candidate decision')
        decision = self._last_decision
        if decision is None:
            raise ValueError('keep requires an issued full-candidate decision')
        if decision.action != EnhancementAction.KEEP_AND_LOCK or slot != decision.replacement_slot:
            raise ValueError('keep does not match the joint decision')
        before = self.build
        self._campaign.build = self._full_preview[0]
        result = decision.replacement_slot or 'inventory'
        self._finish()
        self.emit('kept', dict(identity=asdict(identity), state=asdict(state), slot=result,
                               score_gain=self.build.score-before.score, equipment_applied_in_game=False))
        return result

    def record_discard(self, identity, state, reason):
        if self.active_identity != identity or self.active_state != state:
            raise ValueError('discard must match the active candidate')
        pending = super().record_discard(identity, state, reason)
        self._finish()
        self.emit('discarded', dict(identity=asdict(identity), state=asdict(state), reason=reason,
                                    recoverable_later=asdict(pending.recoverable_later), refunded_to_cash=False))
        return pending

    def synchronize_resources(self, snapshot):
        before = self.ledger.snapshot()
        super().synchronize_resources(snapshot)
        self.emit('resource_observation', dict(before=before, observed=asdict(snapshot),
            detail='Authoritative balance correction; modeled gross costs are retained separately.'))

    def stop(self, reason, *, error=None):
        self.stop_reason = reason
        self.emit('stopped', dict(reason=reason, error=error))


def simulate_adaptive_session(session: AdaptiveEnhancementSession, attempts: int) -> CampaignResult:
    """Use the same scheduler/decisions as live mode, with seeded fake rolls."""
    initial = session.build
    simulator = EchoUpgradeSimulator(seed=session.config.outcome_seed)
    events = []
    successes = failures = 0
    stop_reason = 'maximum attempts reached'
    session.emit('simulation_stock_assumption', dict(
        unknown_stock='unlimited for this simulation only; provide inspection.fresh_stock to model scarcity'))

    def event(action, identity, state, reason, before):
        events.append(CampaignEvent(len(events)+1, session.candidate_number, action,
            identity.name, identity.cost, state.level, reason, before, session.ledger.snapshot()))

    for _ in range(attempts):
        selection = session.choose_candidate()
        if selection.selected is None:
            stop_reason = selection.reason
            break
        identity = selection.selected
        session.begin_candidate(identity)
        state = EchoState()
        event('start', identity, state, selection.reason, session.ledger.snapshot())
        while True:
            decision = session.advise(identity, state)
            before = session.ledger.snapshot()
            if decision.action == EnhancementAction.STOP_RESOURCES:
                stop_reason = decision.reason
                event('discard_resource_stop', identity, state, decision.reason, before)
                session.stop(stop_reason)
                return CampaignResult(initial, session.build, session.ledger, tuple(events),
                    session.candidate_number, successes, failures, stop_reason, session.config)
            if decision.action == EnhancementAction.MARK_DISCARD:
                session.record_discard(identity, state, decision.reason)
                failures += 1
                event('discard_failure', identity, state, decision.reason, before)
                break
            if decision.action == EnhancementAction.KEEP_AND_LOCK:
                session.record_keep(identity, state, decision.replacement_slot)
                successes += 1
                event('replace_success' if decision.score_gain > 0 else 'keep_reserve',
                      identity, state, decision.reason, before)
                break
            observed = state.add(simulator.reveal_next(state))
            session.record_reveal(state, observed)
            state = observed
            event('continue', identity, state, decision.reason, before)
    session.stop(stop_reason)
    return CampaignResult(initial, session.build, session.ledger, tuple(events),
        session.candidate_number, successes, failures, stop_reason, session.config)
