"""Terminal task outcomes remain separate from agent availability."""

import json

from skcoord.coordination import AgentFile, Board, Task


def test_link_completion_projects_idle_without_touching_live_board(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")
    live = tmp_path / "live"
    live.mkdir()
    marker = live / "link.json"
    marker.write_bytes(b'{"state":"active"}\n')

    board = Board(tmp_path / "candidate")
    board.create_task(Task(id="link-done", title="Link work"))
    board.save_agent(
        AgentFile(
            agent="pi-link-chiap08-link-done",
            current_task="link-done",
            claimed_tasks=["link-done"],
        )
    )

    board._complete_task("pi-link-chiap08-link-done", "link-done")

    projection = json.loads(
        board.agent_projection_path("pi-link-chiap08-link-done").read_text()
    )
    assert projection["state"] == "idle"
    assert projection["completed_tasks"] == ["link-done"]
    assert marker.read_bytes() == b'{"state":"active"}\n'


def test_malformed_legacy_terminal_state_is_normalized_before_serializing(tmp_path):
    board = Board(tmp_path)
    board.ensure_dirs()
    path = board.agents_dir / "pi-link-chiap08-legacy.json"
    path.write_text(
        json.dumps(
            {
                "agent": "pi-link-chiap08-legacy",
                "state": "completed",
                "completed_tasks": ["legacy-done"],
            }
        )
    )

    agent = board.load_agent("pi-link-chiap08-legacy")
    assert agent is not None
    board.save_agent(agent)

    assert json.loads(path.read_text())["state"] == "idle"


def test_completion_preserves_offline_availability(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")
    board = Board(tmp_path)
    board.create_task(Task(id="offline-done", title="Offline work"))
    board.save_agent(
        AgentFile(
            agent="pi-link-chiap08-offline",
            state="offline",
            current_task="offline-done",
            claimed_tasks=["offline-done"],
        )
    )

    completed = board._complete_task("pi-link-chiap08-offline", "offline-done")

    assert completed.state.value == "offline"
    assert (
        json.loads(board.agent_projection_path(completed.agent).read_text())["state"]
        == "offline"
    )
