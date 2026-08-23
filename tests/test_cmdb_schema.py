import json
from pathlib import Path

import pytest

from skcoord.cmdb import (
    CMDB_CORE_SCHEMA_VERSION,
    CMDB_EVENT_SCHEMA_VERSION,
    CMDBManager,
    FutureSchemaVersionError,
)


def test_new_core_and_events_are_versioned(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    ci = mgr.create_ci("alpha", "host")
    mgr.set_attribute(ci.id, "test", "answer", 42)

    core = json.loads((tmp_path / "cmdb" / ci.id / "core.json").read_text())
    event_path = next((tmp_path / "cmdb" / ci.id / "events").glob("*.jsonl"))
    event = json.loads(event_path.read_text().splitlines()[0])
    assert core["schema_version"] == CMDB_CORE_SCHEMA_VERSION
    assert event["schema_version"] == CMDB_EVENT_SCHEMA_VERSION


def test_unversioned_v1_fixture_migrates_on_read_and_accepts_v2_events(tmp_path: Path) -> None:
    record = tmp_path / "cmdb" / "ci-host-legacy"
    (record / "events").mkdir(parents=True)
    (record / "core.json").write_text(
        json.dumps(
            {
                "id": "ci-host-legacy",
                "ci_type": "host",
                "name": "legacy",
                "attributes": {"old": True},
                "created_at": "2025-01-01T00:00:00Z",
            }
        )
    )
    mgr = CMDBManager(tmp_path)
    legacy = mgr.get_ci("ci-host-legacy")
    assert legacy.schema_version == 1
    assert legacy.attributes["old"] is True

    mgr.set_attribute(legacy.id, "migration-test", "new", True)
    migrated = mgr.get_ci(legacy.id)
    assert migrated.attributes["new"] is True
    event_path = next((record / "events").glob("*.jsonl"))
    assert json.loads(event_path.read_text())["schema_version"] == CMDB_EVENT_SCHEMA_VERSION


def test_future_core_and_event_schemas_fail_closed(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    ci = mgr.create_ci("future", "host")
    core_path = tmp_path / "cmdb" / ci.id / "core.json"
    core = json.loads(core_path.read_text())
    core["schema_version"] = CMDB_CORE_SCHEMA_VERSION + 1
    core_path.write_text(json.dumps(core))
    with pytest.raises(FutureSchemaVersionError, match="newer than supported"):
        mgr.get_ci(ci.id)

    core["schema_version"] = CMDB_CORE_SCHEMA_VERSION
    core_path.write_text(json.dumps(core))
    mgr.set_attribute(ci.id, "test", "answer", 42)
    event_path = next((tmp_path / "cmdb" / ci.id / "events").glob("*.jsonl"))
    event = json.loads(event_path.read_text())
    event["schema_version"] = CMDB_EVENT_SCHEMA_VERSION + 1
    event_path.write_text(json.dumps(event) + "\n")
    with pytest.raises(FutureSchemaVersionError, match="newer than supported"):
        mgr.get_ci(ci.id)


def test_migration_preview_is_detached_and_does_not_modify_v1(tmp_path: Path) -> None:
    record = tmp_path / "cmdb" / "ci-host-legacy"
    record.mkdir(parents=True)
    original = {"id": "ci-host-legacy", "ci_type": "host", "name": "legacy"}
    (record / "core.json").write_text(json.dumps(original))
    mgr = CMDBManager(tmp_path)

    preview = mgr.migration_preview("ci-host-legacy")
    assert preview["source_schema_version"] == 1
    assert preview["core"]["schema_version"] == CMDB_CORE_SCHEMA_VERSION
    preview["core"]["name"] = "changed-copy"
    assert json.loads((record / "core.json").read_text()) == original


def _legacy_store(home: Path) -> tuple[Path, Path]:
    record = home / "cmdb" / "ci-host-legacy"
    events = record / "events"
    events.mkdir(parents=True)
    core = record / "core.json"
    core.write_text(json.dumps({"id": "ci-host-legacy", "ci_type": "host", "name": "legacy"}))
    event = events / "old@host.jsonl"
    event.write_text(json.dumps({"ts": "2025-01-01T00:00:00Z", "action": "attribute", "key": "x", "value": 1}) + "\n")
    return core, event


def test_store_migration_is_dry_run_by_default(tmp_path: Path) -> None:
    core, event = _legacy_store(tmp_path)
    before = (core.read_bytes(), event.read_bytes())

    result = CMDBManager(tmp_path).migrate_schema()

    assert result["applied"] is False
    assert result["cores"] == 1
    assert result["events"] == 1
    assert (core.read_bytes(), event.read_bytes()) == before
    assert list(tmp_path.glob("cmdb.backup-*")) == []


def test_store_migration_stages_backs_up_and_is_idempotent(tmp_path: Path) -> None:
    core, event = _legacy_store(tmp_path)
    original_core = core.read_bytes()

    result = CMDBManager(tmp_path).migrate_schema(apply=True)

    assert result["applied"] is True
    backup = Path(result["backup"])
    assert (backup / "ci-host-legacy" / "core.json").read_bytes() == original_core
    assert json.loads(core.read_text())["schema_version"] == 2
    assert json.loads(event.read_text())["schema_version"] == 2
    second = CMDBManager(tmp_path).migrate_schema(apply=True)
    assert second["applied"] is False
    assert second["records_to_migrate"] == 0
    assert list(tmp_path.glob("cmdb.backup-*")) == [backup]


def test_store_migration_future_version_fails_before_writes(tmp_path: Path) -> None:
    core, _ = _legacy_store(tmp_path)
    data = json.loads(core.read_text())
    data["schema_version"] = CMDB_CORE_SCHEMA_VERSION + 1
    core.write_text(json.dumps(data))

    with pytest.raises(FutureSchemaVersionError):
        CMDBManager(tmp_path).migrate_schema(apply=True)

    assert json.loads(core.read_text())["schema_version"] == CMDB_CORE_SCHEMA_VERSION + 1
    assert list(tmp_path.glob("cmdb.backup-*")) == []


def test_store_migration_rejects_malformed_event_before_writes(tmp_path: Path) -> None:
    _, event = _legacy_store(tmp_path)
    event.write_text("{broken\n")

    with pytest.raises(json.JSONDecodeError):
        CMDBManager(tmp_path).migrate_schema(apply=True)

    assert list(tmp_path.glob("cmdb.backup-*")) == []
