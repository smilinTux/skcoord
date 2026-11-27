"""A coordination Task carries exit_gates, non_goals and spec_version into its card."""

from __future__ import annotations

import json

from skcoord.card_store import CardStore, mirror_coord_create, mirror_coord_create_claimed
from skcoord.coordination import Task

GATES = [{"gate": "independent-review", "owner": "seraph"}]


def _core(tmp_path, card_id):
    return json.loads((tmp_path / "cards" / card_id / "core.json").read_text())


def _task(card_id):
    return Task(
        id=card_id,
        title="Exit gate carrier",
        exit_gates=GATES,
        non_goals=["no deployment"],
        spec_version=2,
    )


def test_mirror_create_preserves_exit_gate_fields(tmp_path):
    mirror_coord_create(tmp_path, _task("gatemir01"))
    core = _core(tmp_path, "gatemir01")
    assert core["exit_gates"] == GATES
    assert core["non_goals"] == ["no deployment"]
    assert core["spec_version"] == 2


def test_mirror_create_claimed_preserves_exit_gate_fields(tmp_path):
    mirror_coord_create_claimed(tmp_path, _task("gatemir02"), "maker")
    core = _core(tmp_path, "gatemir02")
    assert core["exit_gates"] == GATES
    assert core["non_goals"] == ["no deployment"]
    assert core["spec_version"] == 2
    assert CardStore(tmp_path).fold("gatemir02").owner == "maker"


def test_plain_task_keeps_legacy_defaults(tmp_path):
    mirror_coord_create(tmp_path, Task(id="gatemir03", title="Legacy"))
    core = _core(tmp_path, "gatemir03")
    assert core["exit_gates"] == []
    assert core["non_goals"] == []
    assert core["spec_version"] is None
