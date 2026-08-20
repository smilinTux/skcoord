from pathlib import Path

import pytest

from skcoord.cmdb import CMDBManager
from skcoord.cmdb_projection import build_snapshot, project


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


def test_projection_is_deterministic_and_sink_only_gets_snapshot(tmp_path: Path) -> None:
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
