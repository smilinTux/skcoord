import json
from pathlib import Path

import pytest

from skcoord.cmdb import CMDBManager
from skcoord.cmdb_projection import (
    AgeProjectionSink,
    JsonCheckpointStore,
    JsonProjectionSink,
    project,
)


def test_json_projection_and_checkpoint_are_durable(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path / "canonical")
    ci = mgr.create_ci("search-api", "service", attributes={"port": 443})
    index_path = tmp_path / "derived" / "search.json"
    checkpoint = JsonCheckpointStore(tmp_path / "state" / "checkpoint.json")

    result = project(mgr, [JsonProjectionSink(index_path)], checkpoint)

    index = json.loads(index_path.read_text())
    assert index["checkpoint"] == result["checkpoint"]
    assert index["item_count"] == 1
    assert index["items"][0]["id"] == ci.id
    assert checkpoint.load() == {
        "schema_version": 1,
        "checkpoint": result["checkpoint"],
        "item_count": 1,
        "sinks": ["JsonProjectionSink"],
    }
    assert result["committed_checkpoint"] == result["checkpoint"]


def test_failed_pipeline_does_not_advance_persistent_checkpoint(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path / "canonical")
    mgr.create_ci("api", "service")
    store = JsonCheckpointStore(tmp_path / "checkpoint.json")
    first = project(mgr, [JsonProjectionSink(tmp_path / "index.json")], store)

    class FailingSink:
        def replace(self, snapshot):
            raise RuntimeError("AGE unavailable")

    mgr.create_ci("worker", "service")
    failed = project(mgr, [JsonProjectionSink(tmp_path / "index.json"), FailingSink()], store)

    assert failed["complete"] is False
    assert failed["committed_checkpoint"] is None
    assert store.load()["checkpoint"] == first["checkpoint"]


def test_checkpoint_write_failure_is_reported_as_projection_lag(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path / "canonical")
    mgr.create_ci("api", "service")

    class FailingCheckpoint:
        def commit(self, checkpoint, item_count, sinks):
            raise OSError("disk full")

    result = project(mgr, [], FailingCheckpoint())

    assert result["complete"] is False
    assert result["committed_checkpoint"] is None
    assert result["failed_sinks"] == [
        {"sink": "FailingCheckpoint", "error": "OSError"}
    ]


def test_age_sink_is_injected_and_receives_only_detached_data(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path / "canonical")
    ci = mgr.create_ci("api", "service", attributes={"nested": {"answer": 42}})

    class RecordingAgeAdapter:
        call = None

        def replace_projection(self, checkpoint, items):
            self.call = (checkpoint, items)
            items[0]["attributes"]["nested"]["answer"] = 0

    adapter = RecordingAgeAdapter()
    result = project(mgr, [AgeProjectionSink(adapter)])

    assert adapter.call[0] == result["checkpoint"]
    assert adapter.call[1][0]["id"] == ci.id
    assert mgr.get_ci(ci.id).attributes["nested"]["answer"] == 42


def test_checkpoint_store_rejects_unknown_schema(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.json"
    path.write_text('{"schema_version": 99}')

    with pytest.raises(ValueError, match="unsupported projection checkpoint"):
        JsonCheckpointStore(path).load()
