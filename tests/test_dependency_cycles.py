"""Cycle detection at write time, not discovery time."""

from __future__ import annotations

import pytest

from skcoord.dependency_graph import would_create_cycle


def test_self_dependency_is_a_cycle():
    assert would_create_cycle({}, "a", "a") is True


def test_direct_two_card_cycle():
    """The measured pathology: parent depends on leaf, leaf on parent."""
    edges = {"leaf": ["parent"]}
    assert would_create_cycle(edges, "parent", "leaf") is True


def test_transitive_cycle():
    edges = {"b": ["c"], "c": ["a"]}
    assert would_create_cycle(edges, "a", "b") is True


def test_diamond_is_not_a_cycle():
    """Two paths to a shared dependency are legal."""
    edges = {"b": ["d"], "c": ["d"]}
    assert would_create_cycle(edges, "a", "b") is False
    assert would_create_cycle(edges, "a", "c") is False


def test_unrelated_edge_is_not_a_cycle():
    assert would_create_cycle({"x": ["y"]}, "a", "b") is False


def test_add_dependency_rejects_a_cycle(tmp_path):
    """Wired through amend_dependency, using the real CardStore/CardCore API.

    The brief's illustrative test used store.create(title=..., kind=...,
    agent=...) and add_dependency(..., agent=...) with no reason. Neither
    matches this repo: CardStore.create takes a CardCore, and
    amend_dependency requires a non-empty reason. Adapted accordingly.
    """
    from skcoord.card_store import CardCore, CardStore, add_dependency

    store = CardStore(tmp_path)
    parent = store.create(
        CardCore(id="parent0001", kind="task", title="parent", created_by="tester")
    )
    leaf = store.create(
        CardCore(id="leaf0001", kind="task", title="leaf", created_by="tester")
    )

    assert (
        add_dependency(tmp_path, leaf, parent, agent="tester", reason="seed dependency")
        is True
    )
    with pytest.raises(ValueError) as excinfo:
        add_dependency(tmp_path, parent, leaf, agent="tester", reason="would cycle")
    assert "cycle" in str(excinfo.value).lower()


def test_create_rejects_a_forward_reference_cycle(tmp_path):
    """create() must catch cycles too: 700 of 5,861 live cards got their
    dependency edges through create(), which never inspected core.dependencies
    for cycles. amend_dependency's guard only ever sees an add_dependency call,
    and this estate has never made one, so the guard as wired never fired.

    A cycle formed entirely at birth: card A is created depending on B before
    B exists (create() never checks dependency existence), then B is created
    depending on A. That closes a cycle without ever calling amend_dependency.
    """
    from skcoord.card_store import CardCore, CardStore

    store = CardStore(tmp_path)
    store.create(
        CardCore(
            id="cardA0001",
            kind="task",
            title="a",
            created_by="tester",
            dependencies=["cardB0001"],
        )
    )
    with pytest.raises(ValueError) as excinfo:
        store.create(
            CardCore(
                id="cardB0001",
                kind="task",
                title="b",
                created_by="tester",
                dependencies=["cardA0001"],
            )
        )
    assert "cycle" in str(excinfo.value).lower()


def test_create_with_no_dependencies_is_unaffected(tmp_path):
    """The overwhelming majority of creates (5,161 of 5,861 live cards) carry
    no dependencies at all, so the cycle check must not cost them a graph
    build."""
    from skcoord.card_store import CardCore, CardStore

    store = CardStore(tmp_path)
    card_id = store.create(
        CardCore(id="plain0001", kind="task", title="plain", created_by="tester")
    )
    assert card_id == "plain0001"


def test_create_allows_a_legitimate_dependency_on_an_existing_card(tmp_path):
    from skcoord.card_store import CardCore, CardStore

    store = CardStore(tmp_path)
    store.create(
        CardCore(id="gate0001", kind="task", title="gate", created_by="tester")
    )
    card_id = store.create(
        CardCore(
            id="leaf0002",
            kind="task",
            title="leaf",
            created_by="tester",
            dependencies=["gate0001"],
        )
    )
    assert card_id == "leaf0002"
