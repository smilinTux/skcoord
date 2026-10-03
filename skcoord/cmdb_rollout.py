"""Operator CLI for safe CMDB ownership and timer cutover gates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .cmdb import CMDBManager
from .cmdb_ownership import (
    apply_ownership_backfill,
    evaluate_shadow_gate,
    load_manifest,
    plan_ownership_backfill,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--home", type=Path, required=True, help="SKCapstone home containing cmdb/"
    )
    parser.add_argument("--artifact", type=Path, action="append", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approval-digest", default="")
    args = parser.parse_args(argv)

    gate = evaluate_shadow_gate(args.artifact)
    plan = plan_ownership_backfill(CMDBManager(args.home), load_manifest(args.manifest), gate)
    if not args.apply:
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0 if plan["eligible"] else 2
    try:
        audit = apply_ownership_backfill(CMDBManager(args.home), plan, args.approval_digest)
    except (ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    print(json.dumps({"applied": True, "audit": str(audit)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
