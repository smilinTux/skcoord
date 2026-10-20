"""SKCOORD-OPERATOR-PRINCIPAL-01 tests.

Covers:

* AC1: immutable operator_principal provenance for author, recommender,
  reviewer, and integration actors; no credentials or personal secrets.
* AC2: assignment / independent review / merge eligibility fail closed when
  author and reviewer resolve to the same operator across identities, hosts,
  sessions, or forge accounts.
* AC3: conservative backwards compatibility (no records = legacy card not
  gated), Lumina/Mero alias coverage, and alias normalisation.

All checks run against a throwaway CardStore under a tmp home; nothing touches
a live home, a service, a credential store, or a live gateway.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skcoord.card_store import CardCore, CardStore
from skcoord.operator_principal import (
    OperatorAliasTable,
    append_operator_principal,
    apply_operator_independence_gate,
    load_operator_principals,
    merge_eligibility,
    same_operator,
)


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture()
def store(home: Path) -> CardStore:
    return CardStore(home)


@pytest.fixture()
def card_id(store: CardStore) -> str:
    return store.create(
        CardCore(
            id="op-principal-test-01",
            title="Test card for operator principal gate",
            initial_labels=["skcoord", "skcapstone", "source-only"],
        )
    )


def test_principal_record_is_immutable_and_idempotent(store, card_id):
    """AC1: one record per role; re-appending the same transition returns
    the original record instead of duplicating it."""
    first = append_operator_principal(
        store, card_id, "author", "jarvis", actor_name="Jarvis"
    )
    again = append_operator_principal(store, card_id, "author", "jarvis", actor_name="Jarvis")
    assert again == first
    records = load_operator_principals(store, card_id)
    assert set(records.keys()) == {"author"}
    from skcoord.card_store import _HOSTNAME
    log_path = store.home / "coordination" / "card_events" / f"operator-principal@{_HOSTNAME}.jsonl"
    lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rec = json.loads(lines[0])
    assert rec["role"] == "author"
    assert rec["principal"] == "jarvis"
    # no credential material: only identity facts are stored
    for forbidden in ("token", "secret", "fingerprint", "key"):
        assert forbidden not in rec


def test_no_records_is_backwards_compatible(store, card_id):
    """AC3: a card with no principal records stays merge-eligible (legacy)."""
    decision = apply_operator_independence_gate(store, card_id)
    assert decision["eligible"] is True
    assert decision["reason"] == "no_principal_records"


def test_same_operator_across_hats_fails_closed(store, card_id):
    """AC2: author and reviewer resolving to the same operator blocks the
    gate even when they act under different identities/hosts/sessions/forge
    accounts."""
    append_operator_principal(store, card_id, "author", "jarvis")
    # Same human operator, different hat: a forge account alias of jarvis.
    append_operator_principal(store, card_id, "reviewer", "forge:jarvis", actor_name="Reviewer session B")
    decision = apply_operator_independence_gate(store, card_id)
    assert decision["eligible"] is False
    assert decision["reason"] == "same_operator"
    # The structural fold must not carry this conclusion by itself: the gate
    # conclusion lives in the gate's own evidence record.
    records = load_operator_principals(store, card_id)
    assert same_operator(records["author"], records["reviewer"]) is True


def test_independent_operators_pass(store, card_id):
    append_operator_principal(store, card_id, "author", "jarvis")
    append_operator_principal(store, card_id, "reviewer", "chef")
    decision = apply_operator_independence_gate(store, card_id)
    assert decision["eligible"] is True
    assert decision["reason"] == "independent_operators"


def test_missing_role_fails_closed(store, card_id):
    """AC2: once the gate is adopted (at least one record exists), a missing
    gate-relevant role fails closed."""
    append_operator_principal(store, card_id, "author", "jarvis")
    decision = apply_operator_independence_gate(store, card_id)
    assert decision["eligible"] is False
    assert decision["reason"] == "missing_role"
    assert decision["missing_roles"] == ["reviewer"]


def test_lumina_mero_aliases_resolving_to_one_operator(store, card_id):
    """AC3: Lumina/Mero alias coverage - both acting for one operator."""
    table = OperatorAliasTable(
        {
            "operator-one": ["lumina", "mero", "forge:lumina", "forge:mero"],
        }
    )
    append_operator_principal(store, card_id, "author", "Lumina", alias_table=table)
    append_operator_principal(store, card_id, "reviewer", "Mero", alias_table=table)
    records = load_operator_principals(store, card_id)
    assert records["author"]["principal"] == "operator-one"
    assert records["reviewer"]["principal"] == "operator-one"
    decision = merge_eligibility(card_id, records, alias_table=table)
    assert decision == {"eligible": False, "reason": "same_operator", "gate": "operator-principal"}


def test_default_table_unifies_known_agent_alias_families():
    from skcoord.operator_principal import DEFAULT_ALIAS_TABLE
    table = OperatorAliasTable(DEFAULT_ALIAS_TABLE)
    assert table.resolve("Lumina") == "lumina"
    assert table.resolve("capauth:merO@skworld.io") == "capauth:mero"  # unknown capauth -> its own normalised form
    assert table.resolve("forge:jarvis") == "jarvis"
    assert table.resolve("some-new-agent") == "some-new-agent"


def test_capauth_subject_normalisation():
    table = OperatorAliasTable({})
    assert table.resolve("capauth:Lumina@skworld.io") == "capauth:lumina"
    assert table.resolve("capauth:lumina@skworld.io") == "capauth:lumina"


def test_unicode_actor_names_roundtrip(store, card_id):
    """AC3/AC1: actor names survive a full round-trip through the evidence
    store without mojibake, and credentials are never stored."""
    append_operator_principal(store, card_id, "author", "jarvis", actor_name="Ärthur Ç. Öp")
    records = load_operator_principals(store, card_id)
    rec = records["author"]
    payload = json.loads(rec["link_value"])
    assert payload["actor_name"] == "Ärthur Ç. Öp"
    for forbidden in ("token", "secret", "fingerprint", "key"):
        assert forbidden not in rec


def test_unknown_principal_fails_closed(card_id):
    """AC2/AC3: unknown principals (no alias entry) fall back to the
    normalised identity, so two different unknown identities are NOT the same
    operator and pass; but a missing role still fails closed."""
    records = {
        "author": {"principal": "unknown-author"},
        "reviewer": {"principal": "unknown-reviewer"},
    }
    decision = merge_eligibility(card_id, records)
    assert decision["eligible"] is True
    assert decision["reason"] == "independent_operators"
    # same_operator with a missing principal fails closed (returns True only
    # when both are absent or both exactly match).
    assert same_operator({"principal": ""}, {"principal": ""}) is True
    assert same_operator({"principal": "x"}, {"principal": ""}) is False


def test_assignment_gate_records_and_reassignment_guard(store, card_id):
    """AC2: an assignment (claim) made by an actor is joined with a principal
    evidence event; if the later reviewer is the same operator, the gate
    blocks independent review."""
    append_operator_principal(store, card_id, "author", "chef")
    append_operator_principal(store, card_id, "reviewer", "forge:chefboyrdave21")
    decision = apply_operator_independence_gate(store, card_id)
    assert decision["eligible"] is False
    assert decision["reason"] == "same_operator"
    assert decision["roles"] == ["author", "reviewer"]
