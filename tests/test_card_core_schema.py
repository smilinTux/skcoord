"""Tier-one card-core.v1 schema and write-gate coverage."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from skcoord.card_core_schema import (
    load_card_core_v1_schema,
    validate_card_core_files,
    validate_card_core_v1,
)


def valid_core(**updates):
    core = {
        "id": "a1b2c3d4",
        "kind": "task",
        "title": "Schema candidate",
        "description": "",
        "created_by": "",
        "created_at": "2026-08-26T22:00:00+00:00",
        "acceptance_criteria": ["Produce exact evidence"],
        "dependencies": [],
        "initial_priority": "critical",
        "initial_swimlane": "feature",
        "initial_labels": ["schema"],
        "meta": {},
    }
    core.update(updates)
    return core


def test_schema_is_versioned_closed_and_has_exact_universal_keys():
    schema = load_card_core_v1_schema()
    assert schema["$id"] == "https://skworld.io/schemas/card-core.v1.json"
    Draft202012Validator.check_schema(schema)
    assert schema["additionalProperties"] is False
    assert schema["required"] == list(schema["properties"])
    assert len(schema["required"]) == 12


def test_tier_one_accepts_empty_description_and_creator():
    assert validate_card_core_v1(valid_core()) == []


def test_tier_one_reports_offending_extra_key_and_enum():
    failures = validate_card_core_v1(valid_core(disposition="PASS", kind="memo"))
    messages = "\n".join(f"{failure.path}: {failure.message}" for failure in failures)
    assert "Additional properties are not allowed ('disposition' was unexpected)" in messages
    assert "'memo' is not one of" in messages


def test_standalone_validator_names_file_card_and_reason(tmp_path: Path):
    core_path = tmp_path / "cards" / "a1b2c3d4" / "core.json"
    core_path.parent.mkdir(parents=True)
    core_path.write_text(json.dumps(valid_core(disposition="PASS")))
    failures = validate_card_core_files([core_path])
    assert failures[0].card_id == "a1b2c3d4"
    assert failures[0].path == "$"
    assert "'disposition' was unexpected" in failures[0].message
