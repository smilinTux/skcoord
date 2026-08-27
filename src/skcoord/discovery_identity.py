"""Secret-free CapAuth identity estate discovery."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import PurePath
from typing import Any

from .cmdb import CIType, make_ci_id
from .discovery_base import DISCOVERED_TAG, CommandRunner, DiscoveredCI

IDENTITY_EVIDENCE_MAX_AGE = timedelta(hours=24)

_IDENTITY_INVENTORY_SCRIPT = r"""# skcoord-identity-estate-v1
import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path

home = Path.home().resolve()
skhome = Path(os.environ.get("SKCAPSTONE_HOME", home / ".skcapstone")).expanduser()
capauth = skhome / "capauth"
compat = home / ".capauth"

def load(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}

def resolved(path):
    try:
        return str(path.resolve(strict=False))
    except OSError:
        return str(path)

compat_target = ""
try:
    if compat.is_symlink():
        compat_target = resolved(compat)
except OSError:
    pass

syncthing_roots = []
for config in (
    home / ".local" / "state" / "syncthing" / "config.xml",
    home / ".config" / "syncthing" / "config.xml",
):
    try:
        xml = ET.parse(config).getroot()
    except (OSError, ET.ParseError):
        continue
    for folder in xml.findall("folder"):
        raw = str(folder.get("path") or "").strip()
        if not raw:
            continue
        if raw == "~" or raw.startswith("~/"):
            raw = str(home) + raw[1:]
        path = Path(os.path.expandvars(raw))
        if not path.is_absolute():
            path = home / path
        syncthing_roots.append(resolved(path))

manifest = load(capauth / "estate.json")
identities = []
for item in manifest.get("identities", []):
    if not isinstance(item, dict):
        continue
    identities.append({
        "fingerprint": str(item.get("fingerprint") or "").upper(),
        "status": str(item.get("status") or ""),
        "identity_type": str(item.get("identity_type") or ""),
    })

evidence_files = list((capauth / "evidence").glob("estate-*.json"))
evidence_path = max(evidence_files, key=lambda path: path.stat().st_mtime) if evidence_files else None
evidence = load(evidence_path) if evidence_path else {}
roots = []
for raw in evidence.get("roots", []):
    path = Path(str(raw)).expanduser()
    roots.append({"path": resolved(path), "present": path.is_dir()})

findings = []
allowed = {"code", "status", "fingerprint", "path", "identity_type", "classification"}
for item in evidence.get("findings", []):
    if isinstance(item, dict):
        findings.append({key: item[key] for key in allowed if item.get(key) not in (None, "")})

profile = load(capauth / "identity" / "profile.json")
key_info = profile.get("key_info") if isinstance(profile.get("key_info"), dict) else {}
profile_locations = []
for access, field in (("public", "public_key_path"), ("restricted", "private_key_path")):
    raw = key_info.get(field)
    if raw:
        profile_locations.append({"access": access, "path": resolved(Path(str(raw)).expanduser())})

print(json.dumps({
    "schema": "skcoord-identity-estate-v1",
    "user_home": str(home),
    "capauth_home": resolved(capauth),
    "compatibility_home": str(compat),
    "compatibility_is_symlink": compat.is_symlink(),
    "compatibility_target": compat_target,
    "syncthing_roots": sorted(set(syncthing_roots)),
    "evidence": {
        "generated_at": evidence.get("generated_at", ""),
        "overall": evidence.get("overall", ""),
        "roots": roots,
        "findings": findings,
    },
    "manifest_identities": identities,
    "profile": {
        "fingerprint": str(key_info.get("fingerprint") or "").upper(),
        "entity_type": str((profile.get("entity") or {}).get("entity_type") or ""),
        "fqid": str(profile.get("fqid") or ""),
        "updated_at": str(profile.get("updated") or ""),
        "operator_signed_at": str(profile.get("operator_signed_at") or ""),
        "material_locations": profile_locations,
    },
}))
"""


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({str(item) for item in value if str(item).strip()})


def _home_for_root(path: str) -> str:
    """Return the user-home prefix for a root containing ``.skcapstone``."""
    parts = PurePath(path).parts
    try:
        index = parts.index(".skcapstone")
    except ValueError:
        return ""
    return str(PurePath(*parts[:index]))


def _windows_era_path(path: str) -> bool:
    lowered = path.replace("\\", "/").lower()
    return bool(re.match(r"^[a-z]:/users/", lowered)) or "/mnt/c/users/" in lowered


def _expected_role_for_path(path: str, host: str, capauth_home: str) -> str:
    normalized = path.replace("\\", "/").rstrip("/")
    if normalized == capauth_home.replace("\\", "/").rstrip("/"):
        return "service"
    if "/.skcapstone/identities/" in normalized:
        return "human"
    marker = "/.skcapstone/agents/"
    if marker in normalized:
        owner = normalized.split(marker, 1)[1].split("/", 1)[0]
        return "node" if owner.lower() == host.lower() else "service"
    return ""


def _parse_inventory(stdout: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != "skcoord-identity-estate-v1"
    ):
        return None
    return payload


def collect_identity_estate(runner: CommandRunner) -> list[DiscoveredCI]:
    """Collect sanitized identity placement metadata from one target node."""
    stdout = runner.run(["python3", "-c", _IDENTITY_INVENTORY_SCRIPT])
    payload = _parse_inventory(stdout) if stdout else None
    if payload is None:
        return []

    user_home = str(payload.get("user_home") or "")
    capauth_home = str(payload.get("capauth_home") or "")
    evidence = (
        payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    )
    root_rows = evidence.get("roots") if isinstance(evidence.get("roots"), list) else []
    identity_roots = sorted(
        {
            str(row.get("path"))
            for row in root_rows
            if isinstance(row, dict) and row.get("path")
        }
    )
    stale_roots = sorted(
        {
            str(row.get("path"))
            for row in root_rows
            if isinstance(row, dict) and row.get("path") and not row.get("present")
        }
    )
    alternate_homes = sorted(
        {
            found
            for path in identity_roots
            if (found := _home_for_root(path)) and found != user_home
        }
    )
    all_paths = [user_home, capauth_home, *identity_roots]
    host_attributes = {
        "actual_user_home": user_home,
        "canonical_capauth_home": capauth_home,
        "compatibility_capauth_home": str(payload.get("compatibility_home") or ""),
        "compatibility_is_symlink": bool(payload.get("compatibility_is_symlink")),
        "compatibility_target": str(payload.get("compatibility_target") or ""),
        "syncthing_roots": _strings(payload.get("syncthing_roots")),
        "identity_roots": identity_roots,
        "stale_identity_roots": stale_roots,
        "alternate_user_homes": alternate_homes,
        "windows_era_layout": any(_windows_era_path(path) for path in all_paths),
        "identity_last_verified_at": str(evidence.get("generated_at") or ""),
        "identity_evidence_status": str(evidence.get("overall") or ""),
    }
    out = [
        DiscoveredCI(
            ci_type=CIType.HOST.value,
            name=runner.host,
            source="capauth:estate",
            observed=True,
            node=runner.host,
            attributes=host_attributes,
            tags=("identity-estate", DISCOVERED_TAG),
            canonical_name=runner.host,
        )
    ]

    policies = {
        str(item.get("fingerprint") or "").upper(): item
        for item in payload.get("manifest_identities", [])
        if isinstance(item, dict) and item.get("fingerprint")
    }
    locations: dict[str, list[dict[str, str]]] = {}
    flags: dict[str, set[str]] = {}
    for finding in evidence.get("findings", []):
        if not isinstance(finding, dict):
            continue
        fingerprint = str(finding.get("fingerprint") or "").upper()
        if not fingerprint:
            continue
        code = str(finding.get("code") or "")
        path = str(finding.get("path") or "")
        if path and code in {
            "public_key",
            "secret_placement",
            "retired_key",
            "unmanifested_key",
        }:
            access = "public" if code == "public_key" else "restricted"
            locations.setdefault(fingerprint, []).append(
                {"access": access, "path": path}
            )
        if code:
            flags.setdefault(fingerprint, set()).add(code)

    profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else {}
    profile_fingerprint = str(profile.get("fingerprint") or "").upper()
    if profile_fingerprint:
        for item in profile.get("material_locations", []):
            if isinstance(item, dict) and item.get("path"):
                locations.setdefault(profile_fingerprint, []).append(
                    {"access": str(item.get("access") or ""), "path": str(item["path"])}
                )

    for fingerprint in sorted(set(policies) | set(locations) | set(flags)):
        policy = policies.get(fingerprint, {})
        role = str(policy.get("identity_type") or "")
        unique_locations = sorted(
            {
                (
                    str(item.get("access") or ""),
                    str(item.get("path") or ""),
                )
                for item in locations.get(fingerprint, [])
                if item.get("path")
            }
        )
        material_locations = [
            {"access": access, "path": path} for access, path in unique_locations
        ]
        mismatches = sorted(
            {
                expected
                for item in material_locations
                if item["access"] == "restricted"
                and (
                    expected := _expected_role_for_path(
                        item["path"], runner.host, capauth_home
                    )
                )
                and role
                and expected != role
            }
        )
        attributes = {
            "fingerprint": fingerprint,
            "expected_signer_role": role,
            "lifecycle_status": str(policy.get("status") or ""),
            "material_locations": material_locations,
            "last_verified_at": str(evidence.get("generated_at") or ""),
            "evidence_status": str(evidence.get("overall") or ""),
            "duplicate_restricted_material": "duplicate_secret"
            in flags.get(fingerprint, set()),
            "signer_role_mismatches": mismatches,
        }
        out.append(
            DiscoveredCI(
                ci_type=CIType.CREDENTIAL.value,
                name=f"{runner.host}:{fingerprint}",
                source="capauth:estate",
                observed=True,
                node=runner.host,
                attributes=attributes,
                tags=("capauth", "identity-placement", DISCOVERED_TAG),
                relationships=(
                    ("runs_on", make_ci_id(CIType.HOST.value, runner.host)),
                ),
            )
        )
    return out


def identity_estate_drift(discovered: list[DiscoveredCI]) -> list[tuple[str, str, str]]:
    """Return ``(ci_id, kind, detail)`` tuples for identity estate drift."""
    findings: list[tuple[str, str, str]] = []
    now = datetime.now(timezone.utc)
    for item in discovered:
        attributes = item.attributes
        if item.ci_type == CIType.HOST.value:
            for home in attributes.get("alternate_user_homes", []):
                findings.append(
                    (item.ci_id, "alternate_home", f"identity root uses {home}")
                )
            for root in attributes.get("stale_identity_roots", []):
                findings.append(
                    (item.ci_id, "stale_identity_root", f"missing root {root}")
                )
            if attributes.get("windows_era_layout"):
                findings.append(
                    (
                        item.ci_id,
                        "windows_era_node",
                        "identity root uses Windows layout",
                    )
                )
            verified = str(attributes.get("identity_last_verified_at") or "")
            try:
                timestamp = datetime.fromisoformat(verified.replace("Z", "+00:00"))
            except ValueError:
                timestamp = None
            if timestamp is None or now - timestamp > IDENTITY_EVIDENCE_MAX_AGE:
                findings.append(
                    (
                        item.ci_id,
                        "stale_identity_root",
                        "identity estate verification is stale",
                    )
                )
        elif item.ci_type == CIType.CREDENTIAL.value:
            if attributes.get("duplicate_restricted_material"):
                findings.append(
                    (
                        item.ci_id,
                        "duplicate_private_key",
                        "restricted material has duplicate placements",
                    )
                )
            mismatches = attributes.get("signer_role_mismatches", [])
            if mismatches:
                findings.append(
                    (
                        item.ci_id,
                        "service_identity_mismatch",
                        "signer role does not match placement role: "
                        + ", ".join(mismatches),
                    )
                )
    return findings
