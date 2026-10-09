"""Offline, reproducible joint-inventory and stopping-policy assessment."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from itertools import product
import json
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.rebuild_echo_check_from_scan import inventory_item
from src.echo.optimizer import EchoIdentity, NamedEchoBuild, NamedEquippedEcho
from src.echo.simulation import EchoState, EchoRoll, EchoStat, EchoUpgradeSimulator, ResourceCostModel
from src.echo.workflow import named_echo_build_from_dict, named_echo_build_to_dict

ER = EchoStat.ENERGY_REGEN


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def state(data):
    return EchoState(tuple(EchoRoll(EchoStat(r['stat']), r['tier']) for r in data['rolls']))


def choices(build, items):
    return [[e for e in items if e.identity.cost == slot.identity.cost
             and e.identity.compatibility == slot.identity.compatibility] for slot in build.echoes]


def joint_best(build, items):
    best = build
    for combo in product(*choices(build, items)):
        if len({e.identity.name for e in combo}) != len(combo):
            continue
        candidate = NamedEchoBuild(build.profile, build.base_panel, tuple(
            NamedEquippedEcho(slot.slot, e.identity, e.state)
            for slot, e in zip(build.echoes, combo)))
        if candidate.profile.is_feasible(candidate.panel) and candidate.score > best.score + 1e-9:
            best = candidate
    return best


def linear_score(build, panel):
    return sum(value * build.profile.weights[stat] for stat, value in panel.values.items() if stat != ER)


def joint_gain_samples(build, items, identity, samples):
    """Exact complement envelope; vectorize only the existing ER utility."""
    # At a fixed ER value, all other score terms are linear, so retaining the
    # highest linear score is exact for this profile, not a heuristic prune.
    assert build.profile.energy_policy is not None
    pools = choices(build, items)
    envelope = {}
    for index, slot in enumerate(build.echoes):
        if (slot.identity.cost, slot.identity.compatibility) != (identity.cost, identity.compatibility):
            continue
        others = [pool for i, pool in enumerate(pools) if i != index]
        for combo in product(*others):
            names = [e.identity.name for e in combo]
            if identity.name in names or len(set(names)) != len(names):
                continue
            panel = build.base_panel
            for echo in combo:
                panel = panel.add_echo(echo.state)
            energy = round(panel.value(ER), 8)
            envelope[energy] = max(envelope.get(energy, -float('inf')), linear_score(build, panel))
    candidate_linear = np.array([sum(r.value * build.profile.weights[r.stat] for r in s.rolls if r.stat != ER) for s in samples])
    candidate_energy = np.array([sum(r.value for r in s.rolls if r.stat == ER) for s in samples])
    policy = build.profile.energy_policy
    weight = build.profile.weights[ER]
    gains = np.zeros(len(samples))
    for energy, linear in envelope.items():
        total = candidate_energy + energy
        value = np.maximum(total, policy.base)
        utility = weight * (np.minimum(value, policy.required) - policy.base)
        utility += weight * policy.tolerance_weight_ratio * np.clip(value - policy.required, 0, policy.comfort - policy.required)
        width = policy.overflow - policy.comfort
        if width:
            distance = np.clip(value - policy.comfort, 0, width)
            utility += weight * policy.tolerance_weight_ratio * (distance - distance * distance / (2 * width))
        delta = linear + candidate_linear + utility - build.score
        gains = np.maximum(gains, np.where(total + 1e-12 >= policy.required, delta, 0))
    gains[gains < 1e-9] = 0
    return gains


def assess(root, sample_count=20000):
    initial = named_echo_build_from_dict(read(root / 'snapshot/current_build.json'))
    items = [NamedEquippedEcho(str(i), e.identity, e.state) for i, raw in enumerate(read(root / 'snapshot/plan.json')['inventory']) for e in [inventory_item(raw)]]
    outcomes = read(root / 'bulk/progress.json')['outcomes']
    pool = [EchoIdentity(**raw) for raw in read(root / 'fresh_scan/result.json')['candidates']]
    pool += [initial.echoes[0].identity]  # 4C is absent from the fresh inventory.
    counts = Counter(e['identity']['name'] for e in outcomes)
    model = ResourceCostModel()
    sim = EchoUpgradeSimulator(seed=20261009)
    samples = tuple(sim.upgrade_to(EchoState(), 5).final for _ in range(sample_count))
    current = initial
    trajectory = []
    checkpoints = {}
    spent_tuners = spent_exp = 0

    def checkpoint(number):
        rows = []
        for identity in pool:
            gains = joint_gain_samples(current, items, identity, samples)
            probability = float(np.mean(gains > 0))
            mean = float(np.mean(gains))
            gross = model.upgrade_cost(0, 5)
            refund = model.recycle_refund(5)
            tuners = gross.tuners - (1 - probability) * refund.tuners
            exp = gross.echo_exp - (1 - probability) * refund.echo_exp
            pressure = max(tuners / (10748 - spent_tuners), exp / (11014000 - spent_exp))
            rows.append(dict(name=identity.name, cost=identity.cost, remaining=counts[identity.name],
                success_probability=probability, expected_gain=mean,
                mean_gain_95_halfwidth=float(1.96 * np.std(gains, ddof=1) / np.sqrt(sample_count)),
                expected_net_tuners=tuners, expected_net_exp=exp, efficiency=mean / pressure))
        best = max(r['efficiency'] for r in rows)
        for row in rows:
            row['relative_efficiency'] = row['efficiency'] / best if best else 0
        checkpoints[str(number)] = dict(score=current.score, rows=rows,
            build=named_echo_build_to_dict(current),
            slot_scores={e.slot: current.contextual_slot_score(e.slot) for e in current.echoes})
        print(f'checkpoint {number}: {current.score:.4f}', flush=True)

    checkpoint(0)
    for entry in outcomes:
        number = entry['number']
        identity = EchoIdentity(**entry['identity'])
        observed = state(entry['state'])
        counts[identity.name] -= 1
        cost = model.upgrade_cost(0, observed.slot_count)
        spent_tuners += cost.tuners
        spent_exp += cost.echo_exp
        if observed.slot_count == 5:
            before = current.score
            items.append(NamedEquippedEcho(str(number), identity, observed))
            current = joint_best(current, items)
            trajectory.append(dict(number=number, name=identity.name, old_outcome=entry['outcome'],
                joint_score=current.score, joint_gain=current.score-before))
        if number in (29, 90, 142, 170):
            checkpoint(number)
    # These are realized suffix costs, not counterfactual predictions: unseen
    # rolls and changed decisions cannot be reconstructed from this journal.
    suffixes = []
    for stop in (29, 90, 142):
        suffix = [e for e in outcomes if e['number'] > stop]
        gross = [model.upgrade_cost(0, len(e['state']['rolls'])) for e in suffix]
        refunds = [model.recycle_refund(len(e['state']['rolls'])) for e in suffix if e['outcome'] == 'record_discard']
        suffixes.append(dict(stop_after=stop, attempts=len(suffix),
            gross_tuners=sum(c.tuners for c in gross), gross_exp=sum(c.echo_exp for c in gross),
            old_policy_net_tuners=sum(c.tuners for c in gross)-sum(c.tuners for c in refunds),
            old_policy_net_exp=sum(c.echo_exp for c in gross)-sum(c.echo_exp for c in refunds),
            final_joint_gain=current.score-checkpoints[str(stop)]['score']))
    data = dict(samples=sample_count, seed=20261009, trajectory=trajectory,
        checkpoints=checkpoints, realized_suffix_costs=suffixes,
        final_score=current.score, initial_score=initial.score,
        scope='Offline assessment only; fresh metrics use full completion, not a simulated staged-policy backtest.')
    (root / 'policy_assessment.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return data


if __name__ == '__main__':
    assess(Path(sys.argv[1]).resolve())
