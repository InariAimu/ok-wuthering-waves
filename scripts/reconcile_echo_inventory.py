"""Merge freshly double-read completed items into an explicit check file.

Consumes successful `run_echo_workflow.py stock` results bound to that check.
It never copies old inventory blindly or changes the actual game equipment.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

def reconcile(inspection, audit_paths, output):
    from src.echo.inventory import EchoCharacterInventoryAnalysis, EchoInventoryScanResult, inventory_item_from_dict
    from src.echo.live_workflow import check_digest, write_json
    from src.echo.workflow import EchoInventoryCheckResult
    check = EchoInventoryCheckResult.read(inspection)
    raw_scan = check.inspection['scan']
    if raw_scan.get('issues'):
        raise ValueError('Existing scan issues must be resolved explicitly, not erased by a supplement')
    items = {item.attribute_key: item for item in map(inventory_item_from_dict, raw_scan['items'])}
    sources = []
    for path in audit_paths:
        path = Path(path).expanduser().resolve()
        report = json.loads(path.read_text(encoding='utf-8'))
        audit = report.get('stock_audit', {})
        if (report.get('status') != 'completed' or report.get('phase') != 'stock' or
                not audit.get('double_read_confirmed') or not audit.get('read_only') or
                audit.get('inspection_sha256') != check_digest(inspection) or
                len(audit.get('directions', [])) != 2 or
                not all(row.get('reached_unchanged_fingerprint_end') for row in audit['directions'])):
            raise ValueError('Supplement must be a complete, double-read stock audit for this exact check')
        for row in audit['items']:
            item = inventory_item_from_dict(row)
            if item.level == 25 and check.build.compatible_slots(item.identity):
                if item.state.slot_count != 5:
                    raise ValueError('Supplement contains an incomplete +25 item')
                items.setdefault(item.attribute_key, item)
        sources.append(str(path))
    if not sources:
        raise ValueError('At least one current stock audit is required')
    scan = EchoInventoryScanResult(tuple(items.values()), (), int(raw_scan['pages_scanned']),
        int(raw_scan['duplicate_fingerprints']),
        str(raw_scan['stop_reason']) + '; supplemented by explicit double-read inventory audits')
    replacements = scan.rank_replacements(check.build, minimum_gain=check.policy.minimum_gain,
                                          required_stats=check.policy.required_stats)
    analysis = EchoCharacterInventoryAnalysis(check.sonata_set, scan, replacements,
                                               str(Path(output) / 'inventory_analysis.json'))
    updated = EchoInventoryCheckResult.from_analysis(analysis, build=check.build,
                  candidate_pool=check.candidate_pool, policy=check.policy)
    updated = replace(updated, inspection=dict(updated.inspection,
        completed_inventory_reconciliation=dict(source_check=str(Path(inspection).resolve()),
            double_read_audits=sources, equipment_applied_in_game=False)))
    write_json(Path(output) / 'inventory_analysis.json', analysis.to_dict())
    return updated.write(Path(output) / 'echo_inventory_check.json')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inspection', type=Path, required=True)
    parser.add_argument('--stock-audits', type=Path, nargs='+', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    from src.echo.live_workflow import write_json
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    try:
        target = reconcile(args.inspection, args.stock_audits, output)
    except Exception as error:
        write_json(output / 'result.json', dict(status='error',
            error=dict(type=type(error).__name__, message=str(error))))
        print(output / 'result.json')
        return 1
    write_json(output / 'result.json', dict(status='completed', inspection=str(target)))
    print(target)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
