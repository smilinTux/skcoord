"""Release workflow regression tests."""

from pathlib import Path


def test_main_push_publish_is_not_waiting_for_a_second_tag_event() -> None:
    """A GITHUB_TOKEN tag push cannot be the only path that uploads to PyPI."""
    workflow = (Path(__file__).parents[1] / ".github/workflows/publish.yml").read_text()
    publish_job = workflow.split("\n  pypi-publish:", 1)[1]
    publish_gate = publish_job.split("\n    runs-on:", 1)[0]

    assert "needs.build.result == 'success'" in publish_gate
    assert "startsWith(github.ref, 'refs/tags/')" not in publish_gate
