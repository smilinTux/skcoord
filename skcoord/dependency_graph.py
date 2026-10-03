"""Dependency cycle detection for the coordination card graph.

Measured 2026-09-16: 23 cycles existed, essentially one pathology. The
SKLEGAL-SECRET-COHORT-SLICE-R1 leaves and their parent "Resolve or quarantine"
cards depended on each other, so 35 open cards could never reach completion and
6 of the 11 worst claim-thrashers were cycle-blocked. Workers claimed, found the
dependency unsatisfiable, released, and repeated.

Detection belongs at write time. The graph is small and the check is cheap, so
there is no reason to discover this later.
"""

from __future__ import annotations


def would_create_cycle(
    edges: dict[str, list[str]], card_id: str, dependency_id: str
) -> bool:
    """True if card_id depending on dependency_id closes a cycle.

    ``edges`` maps a card to the cards it already depends on. The new edge is
    card_id -> dependency_id, so a cycle exists when dependency_id can already
    reach card_id.
    """
    if card_id == dependency_id:
        return True
    seen: set[str] = set()
    stack = [dependency_id]
    while stack:
        current = stack.pop()
        if current == card_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        stack.extend(edges.get(current, ()))
    return False
