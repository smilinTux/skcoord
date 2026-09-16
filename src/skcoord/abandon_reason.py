"""The closed vocabulary for why a worker stopped.

Measured 2026-09-16: 235 of 444 open SKLegal cards (53 percent) had been
claimed and abandoned with no recorded cause. The ledger faithfully recorded
THAT work stopped and never WHY, so more than half the residue was
unattributable. A factory cannot improve what it does not write down.

The vocabulary is deliberately closed and deliberately small. An open text
field would reproduce the current situation with extra steps.
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
    }
)


def validate_abandon_reason(value: str | None) -> str:
    """Return the normalised reason. Absent becomes "unspecified", never raises.

    An UNKNOWN non-empty value still raises, because that is a caller bug worth
    surfacing. An ABSENT value does not, because several dispatcher release paths
    (reapers, stale-claim sweeps) must always succeed. Making those raise would
    strand the claims they exist to free, which is worse than the defect this
    module exists to fix.
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
