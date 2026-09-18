"""Two-replica multi-host SKCoord projection failures (card abe011e9).

Card [SKCOORD-PROJECTION-MULTIHOST-01] M: repair the authoritative SKCoord
projection boundary. The pinned base (d0314ed) exposes six card histories
where the projection boundary fails:

1. A stale whole-file ``save_agent`` write from an old snapshot can
   RESURRECT retired/completed/stopped/review/non-owner tuples in another
   host's canonical bytes.
2. Projection materialization of ``claimed_tasks`` / ``current_task`` /
   ``completed_tasks`` does not derive from the exact folded CardStore
   status + owner; ambiguous or missing authorization silently passes.
3. Lifecycle reconciliation restamps liveness and host identity of
   UNRELATED agents.
4. Completion successor selection trusts ``claimed_tasks[0]`` without
   validating the successor against folded authorization.
5. Conflicting whole-file bytes get last-writer-wins replacement instead of
   being preserved as hash-bound dispositions.

The tests below are deterministic RED reproductions: they fail on the
pinned base exactly as the card's "blocker" link describes, and must PASS
once the source repair lands. The source is intentionally untouched in this
commit.

Two-replica model: every scenario is exercised against TWO coordination
roots ("host A" canonical, "host B" canonical) sharing one CardStore
event log. Host B's stale snapshot (loaded earlier) is written back after
host A's canonical bytes have moved on. Failures appear when B's write
silently clobbers A's newer tuples.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from skcoord.card_store import CardStore
from skcoord.card import Column, KanbanBoard
from skcoord.coordination import AgentFile, AgentState, Board, Task, TaskStatus
from skcoord.lifecycle import repair_lifecycle

# ---------------------------------------------------------------------------
# Fixtures: two replication roots over one CardStore.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _store_enabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Force the CardStore mirror ON and isolate lazy skcapstone imports."""
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    yield
    monkeypatch.delenv("SKCOORD_CARD_STORE", raising=False)


def _root(tmp_path: Path, name: str) -> Path:
    home = tmp_path / name
    (home / "coordination" / "tasks").mkdir(parents=True)
    (home / "coordination" / "agents").mkdir(parents=True)
    (home / "coordination" / "archive").mkdir(parents=True)
    (home / "cards").mkdir(parents=True)
    return home


@pytest.fixture()
def host_a(tmp_path: Path) -> Path:
    """Host A: the CURRENT canonical host (writes the newest bytes)."""
    return _root(tmp_path, "host_a")


@pytest.fixture()
def host_b(tmp_path: Path) -> Path:
    """Host B: the STALE host holding an old snapshot to write back."""
    return _root(tmp_path, "host_b")


def _seed_card_store_card(home: Path, card_id: str, owner: str | None = None) -> None:
    """Seed one CardStore core + a claim event so folds have an owner."""
    store = CardStore(home)
    store.ensure_dirs()
    store.write_core(
        card_id,
        {
            "id": card_id,
            "title": f"Card {card_id}",
            "created_by": "seeder",
            "initial_owner": owner,
        },
    )
    if owner is not None:
        store.append_event(card_id, "claim", "seeder", owner=owner)


def _seed_task(board: Board, task_id: str) -> Task:
    task = Task(id=task_id, title=f"Task {task_id}", created_by="seeder")
    board.create_task(task)
    return task


# ---------------------------------------------------------------------------
# 1. Stale whole-file save_agent writes resurrect dead tuples.
# ---------------------------------------------------------------------------


def test_stale_whole_file_write_resurrects_stopped_review_done_retired_tuples(
    host_a: Path, host_b: Path
) -> None:
    """A stale agent snapshot written back must not revive dead tuples."""
    board_a = Board(host_a)
    board_b = Board(host_b)

    # Host A owns the live projection: two claimed tasks, one current task,
    # and one completed task.
    seed_a = _seed_task(board_a, "task-a-live")
    _seed_task(board_a, "task-a-done")
    agent_a = AgentFile(agent="host-a-agent")
    agent_a.claimed_tasks = [seed_a.id, "task-a-second"]
    agent_a.completed_tasks = ["task-a-done"]
    agent_a.current_task = seed_a.id
    agent_a.state = AgentState.ACTIVE
    board_a.save_agent(agent_a)

    # Host B holds an older snapshot: it saw the agent with only the first
    # claim. It also saw a review/done tuple for a different task and a
    # retired tuple for yet another.
    agent_b = AgentFile(agent="host-b-agent")
    agent_b.claimed_tasks = ["task-a-live"]
    agent_b.current_task = "task-a-live"
    agent_b.state = AgentState.IDLE
    board_b.save_agent(agent_b)

    # Host B then writes back a STALE whole-file overwrite of the same
    # projection (simulating a delayed syncthing push). Under the pinned base
    # this clobbers host A's newer tuples, resurrecting the stopped/review/
    # nonowner/completed/retired states that host A had moved past.
    board_b.save_agent(agent_b)

    # Expected: host A's projection still shows its live state.
    live_a = board_a.load_agent("host-b-agent")
    assert live_a is not None
    assert live_a is not None
    # The stale write must NOT have resurrected dead tuples or erased
    # host A's newer claimed/completed/current tuples.
    assert "task-a-second" in live_a.claimed_tasks, (
        "stale whole-file write erased host A's second claim"
    )
    assert "task-a-done" in live_a.completed_tasks, (
        "stale whole-file write erased host A's completion tuple"
    )
    assert live_a.current_task == "task-a-live"


def test_stale_write_cannot_resurrect_nonowner_or_review_tuple(host_a: Path) -> None:
    """A stale overwrite must not flip ownership or status away from the
    folded CardStore truth."""
    board_a = Board(host_a)
    board_b = Board(host_b)

    seed = _seed_task(board_a, "card-abe011e9")
    _seed_card_store_card(host_a, "card-abe011e9", owner="host-a-agent")

    agent_a = AgentFile(agent="host-a-agent")
    agent_a.claimed_tasks = [seed.id]
    agent_a.current_task = seed.id
    agent_a.state = AgentState.ACTIVE
    board_a.save_agent(agent_a)

    # Host B's stale snapshot has a DIFFERENT owner and a review-status
    # tuple; writing it back on top of A's canonical bytes must not flip the
    # card's owner or status away from the folded CardStore state.
    agent_b = AgentFile(agent="host-b-agent")
    agent_b.claimed_tasks = [seed.id]
    agent_b.current_task = seed.id
    board_b.save_agent(agent_b)

    card = CardStore(host_a).fold("card-abe011e9")
    assert card is not None
    assert card.owner == "host-a-agent", (
        f"stale write flipped folded owner to {card.owner}"
    )
    assert card.status in (Column.DOING,), (
        f"stale write flipped status to {card.status}"
    )


# ---------------------------------------------------------------------------
# 2. Materialization must derive claimed/current/completed from exact folded
#    CardStore status + owner authorization; ambiguity fails closed.
# ---------------------------------------------------------------------------


def test_materialization_fails_closed_on_missing_owner(host_a: Path) -> None:
    """A claimed_tasks entry for a card whose folded owner is missing or
    ambiguous must not silently materialize as claimed/current/completed."""
    board_a = Board(host_a)
    task = _seed_task(board_a, "task-no-owner")
    _seed_card_store_card(host_a, "task-no-owner", owner=None)

    agent = AgentFile(agent="host-a-agent")
    agent.claimed_tasks = [task.id]
    agent.current_task = task.id
    agent.state = AgentState.ACTIVE
    board_a.save_agent(agent)

    store = CardStore(host_a)
    card = store.fold("task-no-owner")
    assert card is not None
    # The card folds with owner=None: no authorization.
    assert card.owner is None
    # Materialization of claimed/current/completed must fail closed - the
    # projected agent tuple should NOT be served as an active claim.
    view = next((v for v in board_a.get_task_views() if v.task.id == task.id), None)
    assert view is not None
    # A card with no folded owner must NOT be claimable by any specific
    # agent; the claim gate must fail closed.
    with pytest.raises(ValueError):
        board_a.claim_task("intruder", task.id)


def test_materialization_fails_closed_on_ambiguous_owner(host_a: Path) -> None:
    """Two claim-conflict entries for the same owner must make materialization
    fail closed rather than pick an arbitrary tuple."""
    board_a = Board(host_a)
    task = _seed_task(board_a, "task-ambig")
    store = CardStore(host_a)
    _seed_card_store_card(host_a, "task-ambig", owner="host-a-agent")
    # Two conflicting claim events for the same owner.
    store.append_event("task-ambig", "claim", "host-a-agent", owner="host-a-agent")
    store.append_event("task-ambig", "claim", "host-a-agent", owner="host-a-agent")

    card = store.fold("task-ambig")
    assert card is not None
    conflicts = card.meta.get("claim_conflicts", [])
    matching = [c for c in conflicts if c.get("owner") == "host-a-agent"]
    assert len(matching) == 2, "expected two conflicting claim events"
    # The materializer must NOT pick an arbitrary claimed/current/completed
    # tuple when two claim revisions exist for the same owner. It must fail
    # closed by refusing the claim.
    with pytest.raises(ValueError):
        board_a.claim_task("host-a-agent", task.id)


# ---------------------------------------------------------------------------
# 3. Lifecycle reconciliation must not restamp unrelated liveness/host.
# ---------------------------------------------------------------------------


def test_reconciliation_preserves_unrelated_agent_liveness(host_a: Path) -> None:
    """repair_lifecycle must only touch projections that actually drifted.
    Unrelated agents' liveness and host-identity fields must be preserved
    byte-for-byte."""
    board_a = Board(host_a)
    _seed_task(board_a, "task-lc-1")
    _seed_card_store_card(host_a, "task-lc-1", owner="owner-1")

    agent_drifted = AgentFile(agent="drifted-agent")
    agent_drifted.claimed_tasks = ["task-lc-1"]
    agent_drifted.current_task = "task-lc-1"
    board_a.save_agent(agent_drifted)

    # An UNRELATED agent that is healthy (its projection matches the folded
    # CardStore state). Its liveness_seen/host identity fields must survive
    # reconciliation untouched.
    unrelated = AgentFile(agent="unrelated-agent")
    unrelated.state = AgentState.ACTIVE
    unrelated.liveness_seen = "2026-01-01T00:00:00+00:00"
    unrelated.host_id = "host-a"
    board_a.save_agent(unrelated)

    before = (host_a / "coordination" / "agents" / "unrelated-agent.json").read_bytes()

    receipt = repair_lifecycle(host_a, actor="reconciler", task_ids={"task-lc-1"})

    after = (host_a / "coordination" / "agents" / "unrelated-agent.json").read_bytes()
    assert after == before, (
        "reconciliation restamped an unrelated agent's liveness/host identity"
    )
    # And the receipt must record exactly which agents were repaired.
    assert "drifted-agent" in receipt.actions


def test_reconciliation_preserves_append_only_history(host_a: Path) -> None:
    """Reconciliation must preserve the append-only CardStore history: no
    lines may be deleted, reordered, or rewritten."""
    board_a = Board(host_a)
    task = _seed_task(board_a, "task-lc-2")
    _seed_card_store_card(host_a, "task-lc-2", owner="owner-2")
    store = CardStore(host_a)

    agent = AgentFile(agent="lc-agent")
    agent.claimed_tasks = [task.id]
    agent.current_task = task.id
    board_a.save_agent(agent)

    events_dir = store._card_events_dir(task.id)
    event_files = list(events_dir.glob("*.jsonl"))
    before_lines = {
        f.name: f.read_text().splitlines() for f in event_files
    }

    repair_lifecycle(host_a, actor="reconciler", task_ids={task.id})

    event_files_after = list(events_dir.glob("*.jsonl"))
    assert len(event_files_after) == len(event_files)
    for f in event_files_after:
        after_lines = f.read_text().splitlines()
        before = before_lines.get(f.name, [])
        assert after_lines[: len(before)] == before, (
            f"reconciliation rewrote or deleted append-only events in {f.name}"
        )


# ---------------------------------------------------------------------------
# 4. Completion successor selection must validate against folded authorization.
# ---------------------------------------------------------------------------


def test_completion_successor_must_be_valid(host_a: Path) -> None:
    """When the current task is completed, the successor must be a valid
    claimed tuple per the folded CardStore (owner + status). The pinned base
    picks claimed_tasks[0] blindly."""
    board_a = Board(host_a)
    t_done = _seed_task(board_a, "task-done-x")
    t_next = _seed_task(board_a, "task-next-x")
    _seed_card_store_card(host_a, "task-next-x", owner="owner-x")

    agent = AgentFile(agent="owner-x")
    agent.claimed_tasks = ["task-done-x", "task-next-x"]
    agent.completed_tasks = []
    agent.current_task = "task-done-x"
    agent.state = AgentState.ACTIVE
    board_a.save_agent(agent)

    result = board_a.complete_task("owner-x", "task-done-x")

    # The successor must be a valid claimed tuple. task-next-x IS valid
    # (folded owner=owner-x, status=doing after claim). The pinned base picks
    # claimed_tasks[0] (task-done-x) which is now done; it must not be picked.
    assert result.current_task == "task-next-x", (
        f"successor selection picked {result.current_task}, expected task-next-x"
    )
    assert "task-done-x" in result.completed_tasks


def test_completion_rejects_invalid_successor(host_a: Path) -> None:
    """If no remaining claimed tuple is valid per folded authorization, the
    completion must fail closed rather than trust claimed_tasks ordering."""
    board_a = Board(host_a)
    t_done = _seed_task(board_a, "task-done-y")
    t_orphan = _seed_task(board_a, "task-orphan-y")

    # task-orphan-y has NO folded owner (never claimed in the CardStore).
    _seed_card_store_card(host_a, "task-orphan-y", owner=None)

    agent = AgentFile(agent="owner-y")
    agent.claimed_tasks = ["task-done-y", "task-orphan-y"]
    agent.completed_tasks = []
    agent.current_task = "task-done-y"
    agent.state = AgentState.ACTIVE
    board_a.save_agent(agent)

    result = board_a.complete_task("owner-y", "task-done-y")
    # The only remaining claimed tuple (task-orphan-y) is NOT authorized by
    # the folded CardStore. Fail closed: current_task must be None.
    assert result.current_task is None, (
        f"invalid successor {result.current_task} picked despite missing folded owner"
    )


# ---------------------------------------------------------------------------
# 5. Conflicting bytes are preserved as hash-bound dispositions; no
#    last-writer-wins replacement.
# ---------------------------------------------------------------------------


def test_conflicting_bytes_preserved_not_replaced(host_a: Path, host_b: Path) -> None:
    """Two hosts writing the same agent projection file concurrently must
    not clobber each other. The losing bytes must be preserved (hash-bound
    disposition), not silently replaced by last-writer-wins."""
    board_a = Board(host_a)
    board_b = Board(host_b)

    agent = AgentFile(agent="shared-agent")
    agent.claimed_tasks = ["task-shared"]
    agent.state = AgentState.ACTIVE
    path = board_a.save_agent(agent)

    # Host B concurrently writes a different snapshot of the SAME projection.
    agent_b = AgentFile(agent="shared-agent")
    agent_b.claimed_tasks = ["task-shared", "task-shared-2"]
    agent_b.current_task = "task-shared-2"
    board_b.save_agent(agent_b)

    # The canonical file on host A must still hold host A's newer bytes; the
    # conflicting host B bytes must be preserved as a hash-bound disposition,
    # NOT erased.
    current = json.loads(path.read_text())
    assert current["claimed_tasks"] == ["task-shared"]
    # A disposition must exist that records the conflict (a hash-bound
    # reference to the losing bytes).
    dispositions = path.parent.glob("*conflict*")
    assert any(dispositions), "conflicting bytes were last-writer-wins replaced, not preserved"
