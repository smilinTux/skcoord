"""Card creation governor regression coverage for review-chain inflation."""

from pathlib import Path

import pytest

from skcoord.card_store import CardCore, CardStore, mirror_coord_create
from skcoord.coordination import Board, Task


def _core(card_id: str, title: str, *labels: str) -> CardCore:
    return CardCore(id=card_id, title=title, initial_labels=list(labels))


def test_second_live_review_for_parent_is_refused_and_names_existing(tmp_path: Path) -> None:
    store = CardStore(tmp_path)
    store.create(_core("parent01", "Implementation"))

    assert store.create(_core("review01", "[REVIEW] First", "parent-parent01")) == "review01"

    with pytest.raises(ValueError, match=r"review01"):
        store.create(_core("review02", "[REVIEW] Duplicate", "parent-parent01"))
    assert store._load_core("review02") is None


def test_terminal_review_allows_rereview_but_third_level_requires_human(tmp_path: Path) -> None:
    store = CardStore(tmp_path)
    store.create(_core("root0001", "Implementation"))
    store.create(_core("review01", "[REVIEW] First", "parent-root0001"))
    store.append_event("review01", "complete", "reviewer")

    assert (
        store.create(_core("review02", "[REVIEW] Re-review", "parent-review01"))
        == "review02"
    )
    store.append_event("review02", "complete", "reviewer")

    with pytest.raises(ValueError, match=r"root0001.*human escalation"):
        store.create(_core("review03", "[REREVIEW] Third", "parent-review02"))
    assert store._load_core("review03") is None
    assert (
        store.create(
            _core(
                "review03",
                "[REREVIEW] Human override",
                "parent-review02",
                "human-override",
            )
        )
        == "review03"
    )


def test_human_override_is_exact_and_non_review_cards_are_unaffected(tmp_path: Path) -> None:
    store = CardStore(tmp_path)
    store.create(_core("parent01", "Implementation"))
    store.create(_core("review01", "[REVIEW] First", "parent-parent01"))

    with pytest.raises(ValueError, match=r"review01"):
        store.create(
            _core(
                "review02",
                "[REVIEW] Not explicitly overridden",
                "parent-parent01",
                "human-override-requested",
            )
        )
    assert (
        store.create(
            _core(
                "review03",
                "[REVIEW] Human override",
                "parent-parent01",
                "human-override",
            )
        )
        == "review03"
    )
    assert (
        store.create(_core("ordinary", "Ordinary follow-up", "parent-parent01"))
        == "ordinary"
    )


def test_repair_dedup_uses_its_own_class(tmp_path: Path) -> None:
    store = CardStore(tmp_path)
    store.create(_core("parent01", "Implementation"))
    store.create(_core("review01", "[REVIEW] Review", "parent-parent01"))
    store.create(_core("repair01", "[REPAIR] Repair", "parent-parent01"))

    with pytest.raises(ValueError, match=r"repair01"):
        store.create(_core("repair02", "[REPAIR] Duplicate", "parent-parent01"))


def test_board_and_mcp_adapter_share_cardstore_refusal_without_legacy_orphans(
    tmp_path: Path,
) -> None:
    board = Board(tmp_path)
    board.create_task(Task(id="parent01", title="Implementation"))
    first = Task(id="review01", title="[REVIEW] CLI", tags=["parent-parent01"])
    board.create_task(first)

    duplicate = Task(id="review02", title="[REVIEW] Duplicate", tags=["parent-parent01"])
    with pytest.raises(ValueError, match=r"review01"):
        board.create_task(duplicate)
    assert not list(board.tasks_dir.glob("review02-*.json"))

    with pytest.raises(ValueError, match=r"review01"):
        mirror_coord_create(
            tmp_path,
            Task(id="review03", title="[REVIEW] MCP", tags=["parent-parent01"]),
        )


def test_cli_and_mcp_paths_refuse_third_rereview_without_human_override(
    tmp_path: Path,
) -> None:
    cli_home = tmp_path / "cli"
    board = Board(cli_home)
    board.create_task(Task(id="cliroot1", title="Implementation"))
    board.create_task(Task(id="clirev01", title="[REVIEW] First", tags=["parent-cliroot1"]))
    CardStore(cli_home).append_event("clirev01", "complete", "reviewer")
    board.create_task(Task(id="clirev02", title="[REVIEW] Second", tags=["parent-clirev01"]))
    CardStore(cli_home).append_event("clirev02", "complete", "reviewer")

    third_cli = Task(id="clirev03", title="[REREVIEW] Third", tags=["parent-clirev02"])
    with pytest.raises(ValueError, match=r"cliroot1.*human escalation"):
        board.create_task(third_cli)
    assert not list(board.tasks_dir.glob("clirev03-*.json"))
    third_cli.tags.append("human-override")
    assert board.create_task(third_cli).is_file()

    mcp_home = tmp_path / "mcp"
    Board(mcp_home).ensure_dirs()
    store = CardStore(mcp_home)
    store.create(_core("mcproot1", "Implementation"))
    store.create(_core("mcprev01", "[REVIEW] First", "parent-mcproot1"))
    store.append_event("mcprev01", "complete", "reviewer")
    store.create(_core("mcprev02", "[REVIEW] Second", "parent-mcprev01"))
    store.append_event("mcprev02", "complete", "reviewer")

    third_mcp = Task(id="mcprev03", title="[REREVIEW] Third", tags=["parent-mcprev02"])
    with pytest.raises(ValueError, match=r"mcproot1.*human escalation"):
        mirror_coord_create(mcp_home, third_mcp)
    assert store._load_core("mcprev03") is None
    third_mcp.tags.append("human-override")
    mirror_coord_create(mcp_home, third_mcp)
    assert store._load_core("mcprev03") is not None
