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
