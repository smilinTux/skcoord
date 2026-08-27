#!/usr/bin/env python3
"""Print gitleaks findings from a report, redacted, so a red gate is actionable.

The secret-scan job used to fail with nothing but "leaks found: 1". That is not
enough to act on: you cannot rotate or purge a secret you cannot locate, so the
only available response was to weaken the gate, which is exactly backwards.

This prints the rule, file, line, commit, author and fingerprint of every
finding, and never the matched value.
"""

from __future__ import annotations

import json
import os
import sys

FIELDS = (
    ("rule", "RuleID"),
    ("file", "File"),
    ("line", "StartLine"),
    ("commit", "Commit"),
    ("author", "Author"),
    ("date", "Date"),
    ("fingerprint", "Fingerprint"),
)


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "gitleaks-report.json"
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        print("no gitleaks report was written")
        return 0
    try:
        rows = json.load(open(path, encoding="utf-8"))
    except ValueError as exc:
        print("gitleaks report is not valid JSON: %s" % exc)
        return 0
    print("total findings: %d" % len(rows))
    for row in rows:
        print("---")
        for label, key in FIELDS:
            print("  %-12s %s" % (label + ":", row.get(key)))
        print("  %-12s %s" % ("secret:", "NOT PRINTED"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
