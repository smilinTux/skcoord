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


def test_cardcore_rejects_gate_missing_owner():
    """The model boundary must reject this, not just validate_exit_gates directly.

    A dict-shaped gate missing owner is exactly as unroutable to the dispatcher
    as the prose string that produced 402 claims on card 06a95c23. It must not
    be constructible.
    """
    with pytest.raises(ValueError):
        CardCore(id="badgate01", title="t", exit_gates=[{"gate": "independent-review"}])


def test_cardcore_rejects_gate_missing_gate_name():
    with pytest.raises(ValueError):
        CardCore(id="badgate02", title="t", exit_gates=[{"owner": "seraph"}])


def test_cardcore_valid_gate_constructs_and_persists(tmp_path):
    """End-to-end: a valid gate survives CardCore construction and CardStore.create."""
    store = CardStore(tmp_path)
    gates = [{"gate": "independent-review", "owner": "seraph"}]
    card_id = store.create(CardCore(id="goodgate01", title="t", exit_gates=gates))
    core = _core_json(tmp_path, card_id)
    assert core["exit_gates"] == gates
