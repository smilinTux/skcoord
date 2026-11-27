"""Read-only CLI that emits the SchedulerTruthV1 contract as JSON.

Usage:
    python3 -m skcoord.scheduler_truth --home ~/.skcapstone [--cards id ...] [--as-of ISO]

The CLI is strictly read-only: it never appends events or mutates the store.
Exit codes: 0 on success (contract produced), 2 on a contract violation
(population invariant failure).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from ._scheduler_truth_impl import evaluate_scheduler_truth


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--home",
        type=Path,
        default=Path.home() / ".skcapstone",
        help="SKCapstone shared home (default: ~/.skcapstone)",
    )
    parser.add_argument(
        "--cards",
        nargs="*",
        default=None,
        help="Optional explicit card ids; defaults to the full task/epic population",
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help="Reference time (ISO-8601) for readiness checks",
    )
    parser.add_argument(
        "--no-reason-actions",
        action="store_true",
        help="Omit the operator reason-action table from the JSON output",
    )
    args = parser.parse_args(argv)

    as_of = None
    if args.as_of:
        try:
            as_of = datetime.fromisoformat(args.as_of)
            if as_of.tzinfo is None:
                as_of = as_of.replace(tzinfo=timezone.utc)
        except ValueError:
            parser.error("--as-of must be a valid ISO-8601 timestamp")

    truth = evaluate_scheduler_truth(args.home, args.cards, as_of=as_of)

    if args.no_reason_actions:
        truth.reason_actions = {}
    payload = truth.model_dump()
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
