#!/usr/bin/env python3
"""Validate the ITIL store against itil-record.v1 and itil-event.v1.

Why this exists: ITIL events key their type as "kind", while CardStore events use
"action". A reader written for one store sees no state in the other. That single
mismatch let 269 CLOSED incidents be treated as assignable work and burned roughly
half of all fleet worker launches on records no agent could ever claim.

Read-only. Exits 1 if any record or event fails. Prints a remediation summary.
"""
import argparse, collections, glob, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
TERMINAL = {"closed", "resolved", "rejected", "cancelled"}


def load(name):
    with open(os.path.join(HERE, name)) as f:
        return json.load(f)


def current_state(events):
    """Derive lifecycle state. ONLY kind=status sets it.

    'to' is overloaded: kind=assignment also writes it, with a person. Deriving
    state from any 'to' reports 9 incidents in a state named after an agent.
    """
    state = None
    for e in sorted(events, key=lambda e: (e.get("ts", ""), e.get("seq", 0))):
        if e.get("kind") == "status":
            t = e.get("to_state") or e.get("to")
            if t:
                state = t
    return state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.expanduser("~/.skcapstone/coordination/itil"))
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        print("jsonschema not installed", file=sys.stderr)
        return 2

    rec = Draft202012Validator(load("itil-record.v1.schema.json"))
    evt = Draft202012Validator(load("itil-event.v1.schema.json"))

    rfail = collections.Counter()
    efail = collections.Counter()
    states = collections.Counter()
    offenders = collections.defaultdict(list)
    nrec = nevt = 0

    for sub in ("incidents", "problems", "changes"):
        for d in sorted(glob.glob(os.path.join(a.root, sub, "*"))):
            cp = os.path.join(d, "core.json")
            rid = os.path.basename(d)
            if os.path.exists(cp):
                nrec += 1
                try:
                    core = json.load(open(cp))
                except (OSError, json.JSONDecodeError):
                    rfail["unparseable core.json"] += 1
                    offenders["unparseable core.json"].append(rid)
                    continue
                for e in rec.iter_errors(core):
                    key = e.message[:70]
                    rfail[key] += 1
                    offenders[key].append(rid)
            events = []
            ed = os.path.join(d, "events")
            if os.path.isdir(ed):
                for f in sorted(os.listdir(ed)):
                    for line in open(os.path.join(ed, f), encoding="utf-8", errors="replace"):
                        if not line.strip():
                            continue
                        nevt += 1
                        try:
                            e = json.loads(line)
                        except json.JSONDecodeError:
                            efail["unparseable event line"] += 1
                            continue
                        events.append(e)
                        for er in evt.iter_errors(e):
                            efail[er.message[:70]] += 1
            states[current_state(events) or "(none)"] += 1

    print("records: %d   events: %d" % (nrec, nevt))
    print("lifecycle states: %s" % dict(states.most_common()))
    live = sum(v for k, v in states.items() if k not in TERMINAL)
    print("NOT in a terminal state (assignable candidates): %d" % live)

    if rfail:
        print("\nrecord violations (remediation backlog):")
        for k, v in rfail.most_common(12):
            ex = ", ".join(offenders[k][:3])
            print("  %-4d %s   e.g. %s" % (v, k, ex))
    if efail:
        print("\nevent violations:")
        for k, v in efail.most_common(12):
            print("  %-4d %s" % (v, k))
    if not rfail and not efail:
        print("\nall records and events valid")
    return 1 if (rfail or efail) else 0


if __name__ == "__main__":
    sys.exit(main())
