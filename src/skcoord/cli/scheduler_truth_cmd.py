"""CLI for SchedulerTruthV1 - read-only JSON and operator reason table."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..scheduler_truth import (
    SchedulerTruthV1,
    format_scheduler_truth_json,
    print_reason_table,
)


def cmd_show_truth(args: argparse.Namespace) -> int:
    """Display scheduler truth as JSON."""
    if not args.truth_file.exists():
        print(f"Error: Truth file not found: {args.truth_file}", file=sys.stderr)
        return 1

    try:
        data = json.loads(args.truth_file.read_text())
        truth = SchedulerTruthV1.from_dict(data)

        if args.pretty:
            print(format_scheduler_truth_json(truth, pretty=True))
        else:
            print(truth.to_json())

        return 0
    except Exception as e:
        print(f"Error reading truth file: {e}", file=sys.stderr)
        return 1


def cmd_reason_table(args: argparse.Namespace) -> int:
    """Print the operator reason table."""
    print_reason_table()
    return 0


def main() -> int:
    """Main entry point for scheduler truth CLI."""
    parser = argparse.ArgumentParser(
        prog="skcoord scheduler-truth",
        description="SchedulerTruthV1 - Canonical scheduler truth and eligibility reasons",
    )
    subparsers = parser.add_subparsers(dest="command", help="Sub-commands")

    # show-truth command
    show_parser = subparsers.add_parser(
        "show-truth",
        help="Display scheduler truth as JSON",
    )
    show_parser.add_argument(
        "truth_file",
        type=Path,
        help="Path to scheduler truth JSON file",
    )
    show_parser.add_argument(
        "--pretty",
        "-p",
        action="store_true",
        help="Pretty-print JSON output",
    )
    show_parser.set_defaults(func=cmd_show_truth)

    # reason-table command
    table_parser = subparsers.add_parser(
        "reason-table",
        help="Print operator reason table",
    )
    table_parser.set_defaults(func=cmd_reason_table)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 0

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
