"""Rebuild a first-step Echo check file from its structured scan only."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.echo.inventory import (
    EchoCharacterInventoryAnalysis,
    EchoInventoryScanIssue,
    EchoInventoryScanResult,
    inventory_item_from_dict as inventory_item,
)
from src.echo.workflow import EchoInventoryCheckResult
from src.task.CheckEchoInventoryTask import CheckEchoInventoryTask


def rebuild(path: Path) -> EchoInventoryCheckResult:
    loaded = EchoInventoryCheckResult.read(path)
    scan_data = loaded.inspection["scan"]
    scan = EchoInventoryScanResult(
        tuple(inventory_item(item) for item in scan_data["items"]),
        tuple(EchoInventoryScanIssue(
            int(issue["cost"]),
            int(issue["page_index"]),
            int(issue["cell_index"]),
            str(issue["reason"]),
            str(issue.get("screenshot", "")),
        ) for issue in scan_data["issues"]),
        int(scan_data["pages_scanned"]),
        int(scan_data["duplicate_fingerprints"]),
        str(scan_data["stop_reason"]),
    )
    scan = CheckEchoInventoryTask.canonicalize_truncated_names(scan)
    build = CheckEchoInventoryTask.reconcile_equipped_identities(
        loaded.build,
        scan.items,
    )
    replacements = scan.rank_replacements(
        build,
        minimum_gain=loaded.policy.minimum_gain,
        required_stats=loaded.policy.required_stats,
    )
    analysis = EchoCharacterInventoryAnalysis(
        loaded.sonata_set,
        scan,
        replacements,
        str(loaded.inspection.get("report", "")),
    )
    candidate_pool = tuple(sorted(
        {
            item.identity for item in scan.items
            if build.compatible_slots(item.identity)
        },
        key=lambda identity: (
            identity.cost,
            identity.compatibility,
            identity.name,
        ),
    ))
    result = EchoInventoryCheckResult(
        loaded.sonata_set,
        build,
        candidate_pool,
        loaded.policy,
        analysis.to_dict(),
        datetime.now(timezone.utc).isoformat(),
        str(path.resolve()),
    )
    result.write(path)
    if analysis.report:
        Path(analysis.report).write_text(
            json.dumps(analysis.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return EchoInventoryCheckResult.read(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    result = rebuild(args.path.expanduser().resolve())
    print(result.source_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
