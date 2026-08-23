"""Regression tests for explicit discovery ownership enrollment."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from skcoord.cmdb import CMDBManager
from skcoord.cmdb_ownership import (
    OwnershipEntry,
    apply_ownership_backfill,
    evaluate_shadow_gate,
    plan_ownership_backfill,
)
from skcoord.discovery import DISCOVERED_TAG


def _artifact(path: Path, scan: str, scope: str, *, complete: bool = True, apply=False) -> Path:
    data = {
        "schema": "skcoord.cmdb.reconcile-run/v1",
        "scan_id": scan,
        "scope_fingerprint": scope,
        "applied": apply,
        "completeness": {"complete": complete},
    }
    payload = json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n"
    path.write_text(payload)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    path.with_suffix(".sha256").write_text(f"{digest}  {path.name}\n")
    return path


def _three(tmp_path: Path, scope: str) -> list[Path]:
    return [_artifact(tmp_path / f"run-{n}.json", f"run-{n}", scope) for n in range(3)]


def test_shadow_gate_requires_three_verified_same_scope_dry_runs(tmp_path: Path) -> None:
    assert evaluate_shadow_gate(_three(tmp_path, "a" * 64)).eligible
    assert not evaluate_shadow_gate([]).eligible


def test_shadow_gate_rejects_apply_incomplete_scope_drift_and_checksum(tmp_path: Path) -> None:
    paths = _three(tmp_path, "b" * 64)
    paths[0] = _artifact(paths[0], "run-0", "b" * 64, apply=True)
    paths[1] = _artifact(paths[1], "run-1", "c" * 64, complete=False)
    paths[2].write_text(paths[2].read_text() + " ")
    gate = evaluate_shadow_gate(paths)
    assert not gate.eligible
    assert any("applied=false" in error for error in gate.errors)
    assert any("incomplete" in error for error in gate.errors)
    assert any("checksum mismatch" in error for error in gate.errors)


def test_backfill_requires_explicit_matching_scope_and_digest(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    mgr = CMDBManager(tmp_path / "home")
    ci = mgr.create_ci("legacy-api")
    scope = "d" * 64
    gate = evaluate_shadow_gate(_three(evidence, scope))
    plan = plan_ownership_backfill(mgr, [OwnershipEntry(ci.id, "systemd:nor", scope)], gate)
    assert plan["eligible"]
    assert mgr.get_ci(ci.id).tags == []
    with pytest.raises(ValueError, match="approval digest"):
        apply_ownership_backfill(mgr, plan, "wrong")
    audit = apply_ownership_backfill(mgr, plan, plan["plan_digest"])
    enrolled = mgr.get_ci(ci.id)
    assert DISCOVERED_TAG in enrolled.tags
    assert enrolled.tag_authorities[DISCOVERED_TAG] == "systemd:nor"
    assert enrolled.attributes["lifecycle_scope"] == scope
    assert audit.exists() and audit.with_suffix(".sha256").exists()


def test_backfill_rejects_already_owned_ci(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    mgr = CMDBManager(tmp_path / "home")
    ci = mgr.create_ci("owned", tags=[DISCOVERED_TAG])
    scope = "e" * 64
    gate = evaluate_shadow_gate(_three(evidence, scope))
    plan = plan_ownership_backfill(mgr, [OwnershipEntry(ci.id, "systemd:nor", scope)], gate)
    assert not plan["eligible"]
    assert any("already discovery-owned" in error for error in plan["errors"])


def test_backfill_rejects_state_changed_after_review_before_any_write(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    mgr = CMDBManager(tmp_path / "home")
    first = mgr.create_ci("first")
    second = mgr.create_ci("second")
    scope = "f" * 64
    gate = evaluate_shadow_gate(_three(evidence, scope))
    entries = [
        OwnershipEntry(first.id, "systemd:nor", scope),
        OwnershipEntry(second.id, "systemd:nor", scope),
    ]
    plan = plan_ownership_backfill(mgr, entries, gate)
    mgr.set_attribute(second.id, "human", "review_note", "changed")

    with pytest.raises(RuntimeError, match="changed after plan review"):
        apply_ownership_backfill(mgr, plan, plan["plan_digest"])
    assert DISCOVERED_TAG not in mgr.get_ci(first.id).tags
