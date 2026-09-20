"""Shared pytest fixtures for skcoord.

The fold warns once per distinct unreadable overlay line PER PROCESS, which is
deliberate: `_MAX_WARNINGS_PER_FILE` caps warnings per fold() call, fold() runs
once per card, and a fleet selector cycle folds thousands of cards, so one bad
line used to produce thousands of identical warnings in a single run.

Process-global state and test isolation are in tension here. Two tests that use
the same fixture file name and line number would otherwise depend on execution
order, and the second one to run would see no warning at all. That is exactly
what happened: `test_valid_json_in_an_invented_schema_is_reported` failed with
`assert 'chiap08.jsonl' in ''` because a neighbouring test had already warned
for the same key.

Clearing the cache between tests keeps the production behaviour intact and
makes each test observe the warning it is actually asserting about.
"""

from __future__ import annotations

import pytest

import skcoord.card as card_module


@pytest.fixture(autouse=True)
def _reset_fold_warning_cache():
    """Give every test a clean warning cache, before and after it runs."""
    card_module._WARNED_LINES.clear()
    yield
    card_module._WARNED_LINES.clear()
