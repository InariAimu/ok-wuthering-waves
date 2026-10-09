"""Evaluate a recorded campaign checkpoint without inventing unseen rolls.

This is a state-level policy check, not a counterfactual staged-policy replay.
Only outcomes at or before --through contribute stats or resource spending.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.rebuild_echo_check_from_scan import inventory_item
from src.echo import (
    AdaptiveEnhancementSession, DecisionJournal, EchoIdentity, EchoResourceSnapshot,
    EchoRoll, EchoState, EchoStat, JointInventory, NamedEquippedEcho, ResourceCostModel,
)
from src.echo.workflow import EchoInventoryCheckResult, named_echo_build_from_dict


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def replay(root, check, stock_path, through):
    initial = named_echo_build_from_dict(read(root/'snapshot/current_build.json'))
    pool = tuple(EchoIdentity(**row) for row in read(root/'fresh_scan/result.json')['candidates'])
    stock = {EchoIdentity(**row['identity']): row['count'] for row in read(stock_path)['items']}
    completed = [NamedEquippedEcho(str(i), item.identity, item.state)
                 for i, raw in enumerate(read(root/'snapshot/plan.json')['inventory'])
                 for item in [inventory_item(raw)]]
    start = next(json.loads(line) for line in (root/'bulk/events.jsonl').read_text(encoding='utf-8').splitlines()
                 if json.loads(line)['kind'] == 'start')['resources']
    tuners, experience = start['tuners'], start['echo_exp']
    model = ResourceCostModel()
    observations = [item for item in read(root/'bulk/progress.json')['outcomes'] if item['number'] <= through]
    if through < 0 or len(observations) != through:
        raise ValueError('--through must be a completed, contiguous checkpoint')
    for item in observations:
        identity = EchoIdentity(**item['identity'])
        if identity not in stock or stock[identity] <= 0:
            raise ValueError('initial stock does not cover the recorded candidate')
        stock[identity] -= 1
        state = EchoState(tuple(EchoRoll(EchoStat(roll['stat']), roll['tier']) for roll in item['state']['rolls']))
        cost = model.upgrade_cost(0, state.slot_count)
        tuners -= cost.tuners
        experience -= cost.echo_exp
        if state.slot_count == 5:
            completed.append(NamedEquippedEcho(str(item['number']), identity, state))
    build = JointInventory(initial, completed).optimize(initial)
    required_identities = set(pool) | {echo.identity for echo in build.echoes}
    groups = {(echo.identity.cost, echo.identity.compatibility) for echo in build.echoes}
    required_identities.update(echo.identity for echo in completed
                               if (echo.identity.cost, echo.identity.compatibility) in groups)
    if not required_identities.issubset(stock):
        raise ValueError('provide initial counts, including zero stock, for every candidate and equipped identity')
    policy = EchoInventoryCheckResult.read(check).policy
    journal = DecisionJournal(root/'adaptive_validation'/f'{datetime.now():%Y%m%d_%H%M%S_%f}_through_{through}')
    session = AdaptiveEnhancementSession(build, pool, completed_inventory=completed,
        resources=EchoResourceSnapshot.manual(tuners=tuners, echo_exp=experience),
        config=policy, availability=stock, journal=journal,
        mode='offline_checkpoint_validation', source=f'{root}; observations through {through}; stock {stock_path}')
    decision = session.choose_candidate()
    session.stop(decision.reason)
    print(json.dumps(dict(through=through, action=decision.action,
        selected=decision.selected.name if decision.selected else None,
        best_legal=decision.best_legal.name if decision.best_legal else None,
        score=session.build.score, log=str(journal.directory/'summary.md')), ensure_ascii=False))
    return decision


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    parser.add_argument('--check', type=Path, required=True)
    parser.add_argument('--stock', type=Path, required=True)
    parser.add_argument('--through', type=int, required=True)
    args = parser.parse_args()
    replay(args.root.resolve(), args.check, args.stock, args.through)
