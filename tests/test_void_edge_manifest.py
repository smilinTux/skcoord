import hashlib
import json
from pathlib import Path

import pytest

from skcoord.void_edge_manifest import build_manifest


def _card(root: Path, card_id: str, dependencies=(), events=()) -> Path:
    directory = root / card_id
    (directory / "events").mkdir(parents=True)
    core = {
        "id": card_id,
        "kind": "task",
        "title": card_id,
        "description": "",
        "created_at": "2026-08-20T00:00:00+00:00",
        "dependencies": list(dependencies),
        "initial_labels": [],
    }
    (directory / "core.json").write_text(json.dumps(core), encoding="utf-8")
    (directory / "events" / "writer@node.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in events), encoding="utf-8"
    )
    return directory


def _assessment(path: Path, edges: list[dict]) -> None:
    value = {
        "schema": "skcoord.lifecycle-reassessment/v1",
        "generated_at": "2026-08-20T02:00:00+00:00",
        "classes": {"void_dependency_edges": edges},
    }
    value["content_sha256"] = hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    path.write_text(json.dumps(value), encoding="utf-8")


def test_manifest_binds_revisions_and_classifies_without_mutation(tmp_path: Path) -> None:
    cards = tmp_path / "cards"
    dependent = _card(cards, "dependent", ["voidcard"])
    _card(cards, "voidcard", events=[{"action": "void", "ts": "2026-08-20T01:00:00Z"}])
    assessment = tmp_path / "assessment.json"
    _assessment(assessment, [{"card_id": "dependent", "void_dependency_id": "voidcard"}])
    before = (dependent / "core.json").read_bytes()

    manifest = build_manifest(cards, tmp_path / "evidence", assessment)

    assert manifest["edge_count"] == 1
    assert manifest["classification_counts"] == {"remove": 1}
    action = manifest["actions"][0]
    assert action["classification"] == "remove"
    assert len(action["dependent_revision"]) == len(action["void_revision"]) == 64
    assert (dependent / "core.json").read_bytes() == before


def test_manifest_rejects_assessment_hash_mismatch(tmp_path: Path) -> None:
    cards = tmp_path / "cards"
    _card(cards, "dependent", ["voidcard"])
    _card(cards, "voidcard", events=[{"action": "void"}])
    assessment = tmp_path / "assessment.json"
    _assessment(assessment, [{"card_id": "dependent", "void_dependency_id": "voidcard"}])
    value = json.loads(assessment.read_text())
    value["generated_at"] = "drift"
    assessment.write_text(json.dumps(value))

    with pytest.raises(ValueError, match="content_sha256"):
        build_manifest(cards, tmp_path / "evidence", assessment)
