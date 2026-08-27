import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from skcoord.cmdb import CMDBManager, SecretAttributeKeyError
from skcoord.cmdb_projection import build_snapshot, project


def test_concurrent_writer_events_are_locked_and_replayable(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    ci = mgr.create_ci("api", "service")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda value: mgr.set_attribute(ci.id, "scanner", "sample", value),
                range(40),
            )
        )

    event_path = next((tmp_path / "cmdb" / ci.id / "events").glob("*.jsonl"))
    events = [json.loads(line) for line in event_path.read_text().splitlines()]
    assert len(events) == 40
    assert sorted(event["seq"] for event in events) == list(range(40))
    assert mgr.get_ci(ci.id).attributes["sample"] in range(40)


def test_normal_attributes_pass_write_time_secret_guard(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    ci = mgr.create_ci(
        "api",
        attributes={"endpoint": "https://example.invalid", "nested": {"port": 443}},
    )
    mgr.set_attribute(ci.id, "scanner", "health_status", "ok")

    stored = mgr.get_ci(ci.id)
    assert stored.attributes == {
        "endpoint": "https://example.invalid",
        "nested": {"port": 443},
        "health_status": "ok",
    }


def test_substring_secret_key_policy_rejects_csrf_token_name(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)

    with pytest.raises(SecretAttributeKeyError, match="csrf_token_name"):
        mgr.create_ci("api", attributes={"csrf_token_name": "header-name"})

    assert not (tmp_path / "cmdb").exists()


def test_secret_key_rejection_writes_neither_core_nor_event(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    ci = mgr.create_ci("api")

    with pytest.raises(SecretAttributeKeyError, match="API_TOKEN"):
        mgr.set_attribute(ci.id, "scanner", "runtime", {"API_TOKEN": "do-not-store"})

    events_dir = tmp_path / "cmdb" / ci.id / "events"
    assert not events_dir.exists()
    assert mgr.get_ci(ci.id).attributes == {}


def test_relationship_audit_and_transitive_impact_are_bounded(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    host = mgr.create_ci("node", "host")
    api = mgr.create_ci("api", "service")
    web = mgr.create_ci("web", "service")
    mgr.add_relationship(api.id, "test", "runs_on", host.id)
    mgr.add_relationship(web.id, "test", "depends_on", api.id)
    mgr.add_relationship(api.id, "test", "depends_on", web.id)
    mgr.add_relationship(web.id, "test", "runs_on", "ci-service-missing")

    assert [item["kind"] for item in mgr.audit_relationships()] == ["dangling_target"]
    impact = mgr.impact_graph(host.id)
    assert [node["id"] for node in impact["dependents"]] == [api.id, web.id]
    assert impact["cycles"]
    assert mgr.impact_graph(host.id, max_nodes=1)["truncated"] is True
    with pytest.raises(ValueError):
        mgr.impact_graph(host.id, max_depth=-1)


def test_relationship_audit_reports_type_and_unknown_relation(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    service = mgr.create_ci("api", "service")
    database = mgr.create_ci("db", "datastore")
    mgr.add_relationship(service.id, "test", "runs_on", database.id)
    mgr.add_relationship(service.id, "test", "invented", database.id)
    assert [item["kind"] for item in mgr.audit_relationships()] == [
        "unknown_relationship",
        "invalid_target_type",
    ]


def test_alias_relationship_is_part_of_integrity_vocabulary(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    old = mgr.create_ci("old-appliance", "device")
    canonical = mgr.create_ci("managed-host", "host")
    mgr.add_relationship(old.id, "test", "alias_of", canonical.id)
    assert mgr.audit_relationships() == []


def test_projection_is_deterministic_and_sink_only_gets_snapshot(
    tmp_path: Path,
) -> None:
    mgr = CMDBManager(tmp_path)
    mgr.create_ci("api", "service")
    first = build_snapshot(mgr)
    assert first == build_snapshot(mgr)

    class Sink:
        received = None

        def replace(self, snapshot):
            self.received = snapshot

    sink = Sink()
    result = project(mgr, [sink])
    assert sink.received == first
    assert result["checkpoint"] == first.checkpoint
    assert result["items"] == 1


def test_projection_isolates_sinks_and_does_not_commit_partial_checkpoint(
    tmp_path: Path,
) -> None:
    mgr = CMDBManager(tmp_path)
    mgr.create_ci("api", "service", attributes={"nested": {"answer": 42}})

    class MutatingSink:
        def replace(self, snapshot):
            snapshot.items[0]["attributes"]["nested"]["answer"] = 0

    class ReadingSink:
        answer = None

        def replace(self, snapshot):
            self.answer = snapshot.items[0]["attributes"]["nested"]["answer"]

    reader = ReadingSink()
    result = project(mgr, [MutatingSink(), reader])
    assert reader.answer == 42
    assert result["complete"] is True
    assert result["committed_checkpoint"] == result["checkpoint"]

    class FailingSink:
        def replace(self, snapshot):
            raise RuntimeError("projection unavailable")

    failed = project(mgr, [FailingSink(), reader])
    assert failed["complete"] is False
    assert failed["committed_checkpoint"] is None
    assert failed["failed_sinks"] == [{"sink": "FailingSink", "error": "RuntimeError"}]
