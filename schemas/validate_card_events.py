#!/usr/bin/env python3
"""Validate the CardStore evidence store and report link_key vocabulary health.

Read-only. Also exposes read_links(), the helper every reader should use instead
of matching link_key literally, because matching literally is what caused the
supersession miss.
"""
import argparse, collections, glob, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from evidence_vocab import canonical, is_valid_for_write, CORE  # noqa: E402


def read_links(card_id, root=None):
    """Return {canonical_key: [(value, ts, writer, raw_key), ...]} for one card.

    Folds every observed spelling to its canonical key. Cross-writer ordering is
    by (ts, writer, event_id): 'seq' is per-writer and ordering by it interleaves
    history wrong on every multi-writer card.
    """
    root = root or os.path.expanduser("~/.skcapstone/coordination/card_events")
    rows = []
    for f in sorted(glob.glob(os.path.join(root, "*.jsonl"))):
        try:
            fh = open(f, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("card_id") != card_id or e.get("action") != "link":
                    continue
                rows.append(e)
    rows.sort(key=lambda e: (e.get("ts", ""), str(e.get("writer", "")), str(e.get("event_id", ""))))
    out = collections.defaultdict(list)
    for e in rows:
        raw = str(e.get("link_key"))
        out[canonical(raw)[0]].append((e.get("link_value"), e.get("ts"), e.get("writer"), raw))
    return dict(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.expanduser("~/.skcapstone/coordination/card_events"))
    ap.add_argument("--card", help="report the folded links for one card and exit")
    ap.add_argument("--strict", action="store_true", help="exit 1 if any key fails the write gate")
    a = ap.parse_args()

    if a.card:
        for k, vals in sorted(read_links(a.card, a.root).items()):
            for v, ts, w, raw in vals:
                note = "" if raw == k else "   (folded from %r)" % raw
                print("%-22s %s%s" % (k, str(v)[:88], note))
        return 0

    keys = collections.Counter()
    bad = collections.Counter()
    events = malformed = 0
    for f in sorted(glob.glob(os.path.join(a.root, "*.jsonl"))):
        for line in open(f, encoding="utf-8", errors="replace"):
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            events += 1
            if e.get("action") == "link":
                raw = str(e.get("link_key"))
                keys[raw] += 1
                ok, reason = is_valid_for_write(raw)
                if not ok:
                    bad[reason] += 1

    status = collections.Counter()
    for k, v in keys.items():
        status[canonical(k)[1]] += v
    tot = sum(keys.values()) or 1
    controlled = status["core"] + status["aliased"] + status["escape"]

    print("events: %d   malformed lines: %d" % (events, malformed))
    print("link events: %d   distinct keys: %d   core vocabulary: %d" % (tot, len(keys), len(CORE)))
    print("controlled: %.1f%%  (core %.1f%%, aliased %.1f%%, escape %.1f%%)  uncontrolled: %.1f%%"
          % (100 * controlled / tot, 100 * status["core"] / tot,
             100 * status["aliased"] / tot, 100 * status["escape"] / tot,
             100 * status["unknown"] / tot))

    print("\nsafety-critical concepts, canonical-only vs folded:")
    for c in ("human_approval", "verdict", "superseded_by", "blocker"):
        hits = [(k, v) for k, v in keys.items() if canonical(k)[0] == c]
        print("  %-16s %4d -> %4d across %d spellings"
              % (c, keys.get(c, 0), sum(v for _, v in hits), len(hits)))

    if bad:
        print("\nwrite-gate violations (new writes would be rejected):")
        for r, n in bad.most_common(8):
            print("  %-6d %s" % (n, r))
    return 1 if (a.strict and bad) else 0


if __name__ == "__main__":
    sys.exit(main())
