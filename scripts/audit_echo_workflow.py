"""Audit explicit current-run journals and write a portable text report."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inspection', type=Path, required=True)
    parser.add_argument('--events', type=Path, nargs='+', required=True,
                        help='Explicit events.jsonl files in execution order')
    parser.add_argument('--output', type=Path, required=True, help='New report directory')
    args = parser.parse_args(argv)
    from src.echo.live_workflow import write_json
    from src.echo.run_audit import audit_runs, write_audit
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    try:
        report = audit_runs(args.inspection, args.events)
        write_audit(output, report)
    except Exception as error:
        write_json(output / 'audit.json', dict(status='error',
                   error=dict(type=type(error).__name__, message=str(error))))
        print(output / 'audit.json')
        return 1
    print(output / 'summary.md')
    return 0 if report['status'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
