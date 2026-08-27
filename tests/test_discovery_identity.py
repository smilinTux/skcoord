"""Hermetic identity estate discovery and drift tests."""

from __future__ import annotations

import json
from pathlib import Path

from skcoord.cmdb import CIType, CMDBManager, is_secret_attribute_key
from skcoord.discovery import collect_identity_estate, drift, reconcile


class IdentityRunner:
    host = "testnode"

    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls: list[list[str]] = []

    def run(self, argv):
        self.calls.append(list(argv))
        return (
            self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        )


def _payload() -> dict:
    fingerprint = "A" * 40
    canonical = "/mnt/c/Users/Alice/.skcapstone/capauth"
    return {
        "schema": "skcoord-identity-estate-v1",
        "user_home": "/mnt/c/Users/Alice",
        "capauth_home": canonical,
        "compatibility_home": "/mnt/c/Users/Alice/.capauth",
        "compatibility_is_symlink": True,
        "compatibility_target": canonical,
        "syncthing_roots": ["/mnt/c/Users/Alice/.skcapstone"],
        "evidence": {
            "generated_at": "2000-01-01T00:00:00Z",
            "overall": "WARN",
            "roots": [
                {"path": canonical, "present": True},
                {
                    "path": "/home/legacy/.skcapstone/agents/testnode/capauth",
                    "present": False,
                },
            ],
            "findings": [
                {
                    "code": "public_key",
                    "status": "OK",
                    "fingerprint": fingerprint,
                    "path": f"{canonical}/identity/public.asc",
                    "identity_type": "service",
                },
                {
                    "code": "secret_placement",
                    "status": "OK",
                    "fingerprint": fingerprint,
                    "path": "/home/legacy/.skcapstone/agents/testnode/capauth/identity/private.asc",
                    "identity_type": "service",
                },
                {
                    "code": "duplicate_secret",
                    "status": "WARN",
                    "fingerprint": fingerprint,
                    "identity_type": "service",
                },
            ],
        },
        "manifest_identities": [
            {
                "fingerprint": fingerprint,
                "status": "active",
                "identity_type": "service",
            }
        ],
        "profile": {
            "fingerprint": fingerprint,
            "entity_type": "ai",
            "fqid": "jarvis@example.invalid",
            "updated_at": "2000-01-01T00:00:00Z",
            "operator_signed_at": "2000-01-01T00:00:00Z",
            "material_locations": [
                {"access": "public", "path": f"{canonical}/identity/public.asc"}
            ],
        },
    }


def _attribute_keys(value: object):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _attribute_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _attribute_keys(child)


def test_collect_identity_estate_emits_secret_free_host_and_placement_assets() -> None:
    runner = IdentityRunner(_payload())

    found = collect_identity_estate(runner)

    assert len(found) == 2
    host = next(item for item in found if item.ci_type == CIType.HOST.value)
    placement = next(item for item in found if item.ci_type == CIType.CREDENTIAL.value)
    assert host.attributes["actual_user_home"] == "/mnt/c/Users/Alice"
    assert host.attributes["canonical_capauth_home"].endswith("/.skcapstone/capauth")
    assert (
        host.attributes["compatibility_target"]
        == host.attributes["canonical_capauth_home"]
    )
    assert host.attributes["syncthing_roots"] == ["/mnt/c/Users/Alice/.skcapstone"]
    assert host.attributes["alternate_user_homes"] == ["/home/legacy"]
    assert placement.attributes["fingerprint"] == "A" * 40
    assert placement.attributes["expected_signer_role"] == "service"
    assert placement.attributes["duplicate_restricted_material"] is True
    assert placement.attributes["signer_role_mismatches"] == ["node"]
    assert {item["access"] for item in placement.attributes["material_locations"]} == {
        "public",
        "restricted",
    }
    assert placement.relationships == (("runs_on", host.ci_id),)
    assert not any(
        is_secret_attribute_key(key)
        for item in found
        for key in _attribute_keys(item.attributes)
    )
    assert "BEGIN PGP" not in json.dumps([item.attributes for item in found])
    assert "skcoord-identity-estate-v1" in runner.calls[0][2]


def test_identity_estate_drift_reports_requested_mismatch_classes() -> None:
    kinds = {
        finding.kind
        for finding in drift(collect_identity_estate(IdentityRunner(_payload())))
    }

    assert {
        "alternate_home",
        "stale_identity_root",
        "windows_era_node",
        "duplicate_private_key",
        "service_identity_mismatch",
    } <= kinds


def test_identity_estate_reconciles_without_secret_redactions(tmp_path: Path) -> None:
    found = collect_identity_estate(IdentityRunner(_payload()))
    mgr = CMDBManager(tmp_path)

    report = reconcile(mgr, found, apply=True)

    assert report.validation_failures == []
    assert report.secret_redaction_findings == []
    assert len(report.created) == 2
    placement = next(
        ci for ci in mgr.list_cis() if ci.ci_type == CIType.CREDENTIAL.value
    )
    assert placement.attributes["fingerprint"] == "A" * 40
    assert placement.attributes["material_locations"][0]["access"] in {
        "public",
        "restricted",
    }


def test_collect_identity_estate_ignores_malformed_or_wrong_schema() -> None:
    assert collect_identity_estate(IdentityRunner("not-json")) == []
    assert collect_identity_estate(IdentityRunner({"schema": "unexpected"})) == []
