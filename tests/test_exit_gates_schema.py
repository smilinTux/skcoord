"""exit_gates, non_goals and spec_version round-trip through CardCore."""

from __future__ import annotations

import json

import pytest

from skcoord.card_store import CardCore, CardStore


def _core_json(tmp_path, card_id):
    return json.loads((tmp_path / "cards" / card_id / "core.json").read_text())


def test_legacy_card_has_no_spec_version(tmp_path):
    """Absent means v1. Never infer v2."""
    store = CardStore(tmp_path)
    card_id = store.create(CardCore(id="legacy01", title="legacy"))
    core = _core_json(tmp_path, card_id)
    assert core.get("spec_version") in (None, 1)


def test_exit_gates_round_trip(tmp_path):
    store = CardStore(tmp_path)
    gates = [{"gate": "independent-review", "owner": "seraph", "ref": "parent-5a7e5f41"}]
    card_id = store.create(
        CardCore(
            id="v2card01",
            title="v2 card",
            exit_gates=gates,
            non_goals=["no deployment"],
            spec_version=2,
        )
    )
    core = _core_json(tmp_path, card_id)
    assert core["exit_gates"] == gates
    assert core["non_goals"] == ["no deployment"]
    assert core["spec_version"] == 2


def test_prose_exit_gate_is_rejected(tmp_path):
    """A prose string cannot be checked mechanically, so it is rejected."""
    from skcoord.abandon_reason import validate_exit_gates

    with pytest.raises(ValueError):
        validate_exit_gates(["independent review PASS before merge"])


def test_exit_gate_without_owner_is_rejected():
    from skcoord.abandon_reason import validate_exit_gates

    with pytest.raises(ValueError):
        validate_exit_gates([{"gate": "independent-review"}])


def test_valid_exit_gate_passes_validation():
    from skcoord.abandon_reason import validate_exit_gates

    gates = [{"gate": "independent-review", "owner": "seraph"}]
    assert validate_exit_gates(gates) == gates


def test_none_exit_gates_is_an_empty_list():
    from skcoord.abandon_reason import validate_exit_gates

    assert validate_exit_gates(None) == []
