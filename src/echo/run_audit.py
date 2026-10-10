"""Text-only audit of explicit adaptive journals, without a game runtime."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path

from src.echo.live_workflow import stock_document, write_json
from src.echo.optimizer import EchoIdentity
from src.echo.workflow import EchoInventoryCheckResult, named_echo_build_to_dict


RESOURCE_FIELDS = ('gross_tuners', 'gross_exp', 'recoverable_tuners_later', 'recoverable_exp_later')


def audit_runs(inspection, event_paths):
    check = EchoInventoryCheckResult.read(inspection)
    model = named_echo_build_to_dict(check.build)
    runs = []
    all_counts = Counter()
    resources = Counter()
    exhausted = {}
    digests = set()
    pending = []
    for source in event_paths:
        source = Path(source).expanduser().resolve()
        raw = source.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest in digests:
            raise ValueError('Duplicate journal supplied; refusing to double-count consumption')
        digests.add(digest)
        events = [json.loads(line) for line in raw.decode('utf-8').splitlines() if line.strip()]
        if not events or events[0]['kind'] != 'session_start':
            raise ValueError(f'{source}: missing session_start')
        if any(event['snapshot']['mode'] != 'live' for event in events):
            raise ValueError(f'{source}: only live journals can establish material and stock evidence')
        original = events[0]['payload']['original_build']
        if original['profile'] != model['profile'] or original['base_panel'] != model['base_panel']:
            raise ValueError(f'{source}: journal and inspection use different scoring models')
        if [event['sequence'] for event in events] != list(range(1, len(events) + 1)):
            raise ValueError(f'{source}: missing, repeated or reordered events')
        snapshot = events[-1]['snapshot']
        counts = Counter(event['kind'] for event in events)
        closed = (not snapshot['unreconciled_upgrade'] and snapshot['active_identity'] is None and
                  counts['upgrade_click_intent'] == counts['upgrade_clicked'] ==
                  counts['reveal'] == counts['reveal_recorded'] and
                  counts['candidate_started'] == counts['kept'] + counts['discarded'] ==
                  snapshot['completed_candidates'])
        if not closed:
            pending.append(str(source))
        totals = {key: sum(float(event['payload']['gross_cost'][field])
                           for event in events if event['kind'] == 'reveal')
                  for key, field in (('gross_tuners', 'tuners'), ('gross_exp', 'echo_exp'))}
        if any(not math.isclose(value, snapshot['resources'][key], abs_tol=1e-6)
               for key, value in totals.items()):
            raise ValueError(f'{source}: reveal costs do not match the final ledger')
        # Infer exhaustion only from a numeric live observation followed by
        # exactly that many reconciled first reveals in this closed session.
        quantities = {}
        used = Counter()
        for event in events:
            payload = event['payload']
            if event['kind'] == 'stock_observation':
                identity = EchoIdentity(**payload['identity'])
                count = payload['count']
                if payload.get('availability') == 'present':
                    exhausted.pop(identity, None)
                if count is not None:
                    quantities[identity] = count
                    used[identity] = 0
                    if count > 0:
                        exhausted.pop(identity, None)
            elif event['kind'] == 'reveal' and not payload['previous']['rolls']:
                used[EchoIdentity(**payload['identity'])] += 1
        if closed:
            for identity, quantity in quantities.items():
                if identity in check.candidate_pool and quantity == used[identity]:
                    exhausted[identity] = dict(identity=asdict(identity), count=0,
                        evidence=f'{source}: verified count {quantity}, reconciled first reveals {used[identity]}',
                        journal_sha256=digest)
        all_counts.update(counts)
        resources.update({key: snapshot['resources'][key] for key in RESOURCE_FIELDS})
        runs.append(dict(events=str(source), counts=dict(counts), ledger_closed=closed,
                         stop_reason=snapshot['stop_reason'], resources=snapshot['resources'],
                         logical_build=snapshot['build'], logical_score=snapshot['build_score']))
    if not runs:
        raise ValueError('At least one journal is required')
    resources['economic_net_tuners'] = resources['gross_tuners'] - resources['recoverable_tuners_later']
    resources['economic_net_exp'] = resources['gross_exp'] - resources['recoverable_exp_later']
    return dict(kind='ok-ww.echo-run-audit', schema_version=1,
                inspection=str(Path(inspection).resolve()), sonata_set=check.sonata_set,
                status='needs_reconciliation' if pending else 'completed',
                pending_journals=pending, runs=runs, counts=dict(all_counts),
                completed=all_counts['kept'] + all_counts['discarded'],
                modeled_resources=dict(resources),
                equipment_applied_in_game=False, equipment_verification='not performed by this offline audit',
                recycling_executed=False, actual_inventory_delta=None,
                verified_missing_stock=stock_document(inspection, [] if pending else exhausted.values()))


def write_audit(output, report):
    output = Path(output)
    write_json(output / 'audit.json', report)
    # No claim about game equipment or real resource balances is inferred
    # from modeled costs and logical builds.
    lines = ['# 声骸强化日志审计', '', f"状态：{report['status']}", '',
             f"套装：{report['sonata_set']}；已完成 {report['completed']} 件。", '',
             f"保留 {report['counts'].get('kept', 0)}；弃置 {report['counts'].get('discarded', 0)}；"
             f"成功读回 {report['counts'].get('reveal_recorded', 0)} 孔。", '',
             '## 模型材料账', '',
             '以下为最低升级成本与预计返还；不代表实际投料、实际回收或贝币消耗。', '']
    lines += [f'- {key}: {value:g}' for key, value in report['modeled_resources'].items()]
    lines += ['', '## 各轮停止状态', '']
    lines += [f"- {run['events']}: {run['stop_reason']}；账本闭合={run['ledger_closed']}；"
              f"逻辑评分={run['logical_score']:.4f}" for run in report['runs']]
    lines += ['', '实装尚未由此工具核验，实际材料余额需单独读回。', '',
              '完整字段见 `audit.json`；已闭合日志证明的缺货记录见 `verified_missing_stock.json`。', '']
    (output / 'summary.md').write_text('\n'.join(lines), encoding='utf-8')
    write_json(output / 'verified_missing_stock.json', report['verified_missing_stock'])
