"""The closed vocabulary for why a worker stopped.

Measured 2026-09-16: 235 of 444 open SKLegal cards (53 percent) had been
claimed and abandoned with no recorded cause. The ledger faithfully recorded
THAT work stopped and never WHY, so more than half the residue was
unattributable. A factory cannot improve what it does not write down.

The vocabulary is deliberately closed and deliberately small. An open text
field would reproduce the current situation with extra steps.

Two members answer different questions and must never be collapsed:

- "unspecified" means the cause is NOT KNOWN. It is the honest default for a
  release site that cannot say why a worker stopped.
- "not-abandoned" means the cause IS known, and it is that the release was NOT
  an abandonment at all: the work finished durably and a verdict was recorded
  before the claim was released. A future reader who merges these two back
  together reproduces the exact coverage blind spot this module exists to
  fix, because a success release would again be indistinguishable from a
  release nobody can explain.
"""

from __future__ import annotations

ABANDON_REASONS = frozenset(
    {
        # The worker cannot satisfy a stated acceptance criterion.
        "criteria-unsatisfiable",
        # A declared dependency is not met.
        "dependency-unsatisfied",
        # The worker lacks a tool, credential, or host-local asset.
        "capability-missing",
        # The worker failed. The message belongs in the event payload.
        "error",
        # Another worker or a human took the work.
        "superseded",
        # The caller did not say. Explicit sentinel, NOT a silent default.
        # Reaper and stale-claim paths must always succeed and often cannot know
        # why a worker stopped, so a path that must not fail needs a
        # representable answer. Measuring the share of this value is how we know
        # the migration is working.
        "unspecified",
        # The release is NOT an abandonment: the work finished durably and a
        # verdict was recorded before the claim was released. This is the one
        # member that means success, not cause-unknown. It exists so a release
        # after a good finish never lands in "unspecified" next to the 235
        # cards nobody can explain; folding the two together is exactly the
        # collapse this field exists to prevent, so keep them distinct.
        "not-abandoned",
    }
)


def validate_abandon_reason(value: str | None) -> str:
    """Return the normalised reason. Absent becomes "unspecified", never raises.

    An UNKNOWN non-empty value still raises, because that is a caller bug worth
    surfacing. An ABSENT value does not, because several dispatcher release paths
    (reapers, stale-claim sweeps) must always succeed. Making those raise would
    strand the claims they exist to free, which is worse than the defect this
    module exists to fix.

    A caller that knows the release followed a durable finish, not a stoppage,
    should pass "not-abandoned" explicitly. It never comes back from an absent
    value; only a caller that actually knows the release was a success gets it.
    """
    if value is None or not str(value).strip():
        return "unspecified"
    normalised = str(value).strip().lower()
    if normalised not in ABANDON_REASONS:
        raise ValueError(
            f"abandon_reason {value!r} is not in the closed vocabulary: "
            + ", ".join(sorted(ABANDON_REASONS))
        )
    return normalised


def validate_exit_gates(gates: object) -> list[dict]:
    """Return the gate list, or raise ValueError.

    Entries are objects, never prose. The dispatcher needs `owner` to route the
    gate to a seat and `gate` to name it. A string such as "independent review
    PASS before merge" cannot be checked mechanically, which is exactly the
    defect that let card 06a95c23 accumulate 402 claims.
    """
    if gates is None:
        return []
    if not isinstance(gates, list):
        raise ValueError("exit_gates must be a list of objects")
    validated = []
    for entry in gates:
        if not isinstance(entry, dict):
            raise ValueError(f"exit_gates entry must be an object, got {entry!r}")
        if not str(entry.get("gate") or "").strip():
            raise ValueError(f"exit_gates entry needs a 'gate' name: {entry!r}")
        if not str(entry.get("owner") or "").strip():
            raise ValueError(f"exit_gates entry needs an 'owner' seat: {entry!r}")
        validated.append(dict(entry))
    return validated
