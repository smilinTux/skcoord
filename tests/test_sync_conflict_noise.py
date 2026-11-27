"""Syncthing conflict copies and repeated fold summaries must not flood logs."""

from __future__ import annotations

import json
import logging

import skcoord.card as card_mod
import skcoord.coordination as coord_mod
from skcoord.card import CardEventLog
from skcoord.coordination import Board, is_sync_conflict_copy


def _board(tmp_path):
    board = Board(tmp_path)
    board.ensure_dirs()
    return board


def test_conflict_name_detection() -> None:
    assert is_sync_conflict_copy("a.sync-conflict-20261011-184807-ANXXYPM.json")
    assert is_sync_conflict_copy("chiap08.sync-conflict-20261011-184807-ANXXYPM.jsonl")
    assert not is_sync_conflict_copy("chiap08.json")
    assert not is_sync_conflict_copy("sync-conflict.json")


def test_load_agents_skips_conflict_copies_silently(tmp_path, caplog) -> None:
    coord_mod._SEEN_CONFLICT_COPIES.clear()
    board = _board(tmp_path)
    (board.agents_dir / "bob.json").write_text(json.dumps({"agent": "bob"}))
    for i in range(3):
        (board.agents_dir / f"bob.sync-conflict-20261011-18480{i}-ANXXYPM.json").write_text(
            json.dumps({"agent": "bob"})
        )
    with caplog.at_level(logging.DEBUG, logger=coord_mod.logger.name):
        first = board.load_agents()
        second = board.load_agents()
    assert [a.agent for a in first] == ["bob"] == [a.agent for a in second]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    debug = [r for r in caplog.records if "Ignoring Syncthing conflict copy" in r.getMessage()]
    assert len(debug) == 3  # once per copy, not once per call


def test_load_tasks_skips_conflict_copies(tmp_path, caplog) -> None:
    coord_mod._SEEN_CONFLICT_COPIES.clear()
    board = _board(tmp_path)
    (board.tasks_dir / "t1.sync-conflict-20261011-184807-ANXXYPM.json").write_text("{not json")
    with caplog.at_level(logging.WARNING, logger=coord_mod.logger.name):
        assert board.load_tasks() == []
    assert not caplog.records


def test_unreadable_summary_logged_once_per_process(tmp_path, caplog) -> None:
    card_mod._WARNED_LINES.clear()
    card_mod._WARNED_SUMMARIES.clear()
    directory = tmp_path / "coordination" / "card_events"
    directory.mkdir(parents=True)
    (directory / "w.jsonl").write_text("\n".join(["{}"] * 8) + "\n")
    log = CardEventLog(tmp_path)
    with caplog.at_level(logging.WARNING, logger=card_mod.logger.name):
        for _ in range(4):
            log.read_all()
    summaries = [r for r in caplog.records if "unreadable lines total" in r.getMessage()]
    assert len(summaries) == 1
    assert len(log.rejected) == 8
