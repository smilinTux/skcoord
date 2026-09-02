"""Read-only JSON command for the scheduler truth contract."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .scheduler_truth import snapshot_json


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="classify folded SKCoord cards")
    parser.add_argument(
        "input", nargs="?", default="-", help="JSON file, or - for stdin"
    )
    args = parser.parse_args(argv)
    source = (
        sys.stdin.read()
        if args.input == "-"
        else Path(args.input).read_text(encoding="utf-8")
    )
    print(snapshot_json(source))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
