"""Regression: ITIL resolve must complete GTD items under the shared store lock.

``_complete_gtd_items`` used to load-append-save ``archive.json`` without
``_store_lock()``. A concurrent writer that had already loaded the archive
then saved over it, and one side's update vanished (it ate two items on chi).
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import threading
import time

import pytest

from skcoord.itil import ITILManager

gtd_tools = pytest.importorskip("skcapstone.mcp_tools.gtd_tools")


@pytest.fixture(autouse=True)
def _no_skcapstone_module_leak():
    """Same leak guard as ``test_cab_bypass_side_effects.py``."""
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name == "skcapstone" or name.startswith("skcapstone."):
            del sys.modules[name]


@pytest.fixture()
def gtd_dir(tmp_path, monkeypatch) -> pathlib.Path:
    d = tmp_path / "gtd"
    d.mkdir()
    for fname in gtd_tools.GTD_STORE_FILES:
        (d / fname).write_text("[]", encoding="utf-8")
    monkeypatch.setattr(gtd_tools, "_gtd_dir", lambda: d)
    # Use the file lock on THIS store, never skos's lock on the real one.
    monkeypatch.setattr(gtd_tools, "_skos_store_lock", None)
    monkeypatch.setattr(gtd_tools, "_spool_owner", lambda *a, **k: None)
    return d


def _read(d: pathlib.Path, name: str) -> list[dict]:
    return json.loads((d / name).read_text(encoding="utf-8"))


def test_completion_waits_for_store_lock_and_keeps_concurrent_archive_write(gtd_dir):
    (gtd_dir / "next-actions.json").write_text(
        json.dumps([{"id": "itil-item", "text": "[ITIL:inc-1] x", "status": "next"}]),
        encoding="utf-8",
    )
    mgr = ITILManager(pathlib.Path(tempfile.mkdtemp()))
    done = threading.Event()

    def _resolve():
        mgr._complete_gtd_items(["itil-item"])
        done.set()

    # Another writer is mid load-modify-save on the archive when ITIL resolves.
    with gtd_tools._store_lock():
        archive = gtd_tools._load_archive()
        worker = threading.Thread(target=_resolve)
        worker.start()
        time.sleep(0.3)
        assert not done.is_set(), "completion must block on the store lock"
        archive.append({"id": "other-writer", "status": "done"})
        gtd_tools._save_archive(archive)
    worker.join(timeout=10)
    assert done.is_set()

    ids = {i["id"] for i in _read(gtd_dir, "archive.json")}
    assert ids == {"other-writer", "itil-item"}
    assert _read(gtd_dir, "next-actions.json") == []


def test_already_archived_item_is_not_archived_twice(gtd_dir):
    (gtd_dir / "archive.json").write_text(
        json.dumps([{"id": "old", "status": "done", "completed_at": "2026-01-01"}]),
        encoding="utf-8",
    )
    ITILManager(pathlib.Path(tempfile.mkdtemp()))._complete_gtd_items(["old"])
    archive = _read(gtd_dir, "archive.json")
    assert [i["id"] for i in archive] == ["old"]
    assert archive[0]["completed_at"] == "2026-01-01"
