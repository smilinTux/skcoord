"""Release workflow regression tests."""

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_main_push_publish_is_not_waiting_for_a_second_tag_event() -> None:
    """A GITHUB_TOKEN tag push cannot be the only path that uploads to PyPI."""
    workflow = (ROOT / ".github/workflows/publish.yml").read_text()
    publish_job = workflow.split("\n  pypi-publish:", 1)[1]
    publish_gate = publish_job.split("\n    runs-on:", 1)[0]

    assert "needs.build.result == 'success'" in publish_gate
    assert "startsWith(github.ref, 'refs/tags/')" not in publish_gate


def test_public_metadata_requires_operator_session_currentness_capauth() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert '"capauth>=0.3.9"' in pyproject
    assert '"capauth>=0.3.8"' not in pyproject
