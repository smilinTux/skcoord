"""B2 staged "Proposed" lane: autopilot-staged cards are hidden from
unblocked/selection and rendered in their own lane; update_task can strip tags
(the release path)."""

from __future__ import annotations

from skcoord.card import _LANE_META, LANE_ORDER, _swimlane_for_tags
from skcoord.coordination import Board, Task


def test_staged_card_excluded_from_unblocked(tmp_path):
    board = Board(tmp_path)
    live = Task(title="buildable", priority="medium", tags=["repo:skos"])
    staged = Task(
        title="proposed",
        priority="medium",
        tags=["repo:skos", "autopilot", "parent:e1", "autopilot-staged"],
    )
    board.create_task(live)
    board.create_task(staged)
    unblocked = board.unblocked_task_ids()
    assert live.id in unblocked  # normal card is selectable
    assert staged.id not in unblocked  # staged card is hidden from selection/build


def test_update_task_remove_tags_strips_stage(tmp_path):
    board = Board(tmp_path)
    t = Task(
        title="proposed",
        priority="medium",
        tags=["repo:skos", "autopilot-staged", "autopilot-untriaged"],
    )
    board.create_task(t)
    board.update_task(t.id, remove_tags=["autopilot-staged", "autopilot-untriaged"])
    reloaded = {x.id: x for x in board.load_tasks()}[t.id]
    assert "autopilot-staged" not in reloaded.tags
    assert "autopilot-untriaged" not in reloaded.tags
    assert "repo:skos" in reloaded.tags  # unrelated tag preserved
    assert t.id in board.unblocked_task_ids()  # now selectable after release


def test_staged_swimlane_is_proposed():
    assert _swimlane_for_tags(["autopilot-staged", "repo:skos"]) == "proposed"
    assert _swimlane_for_tags(["repo:skos"]) == "feature"  # unchanged default
    assert "proposed" in LANE_ORDER and "proposed" in _LANE_META  # render-safe
