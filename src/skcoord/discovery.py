"""Compatibility facade for CMDB discovery.

Implementation is split by responsibility so collectors, reconciliation, and
drift remain independently testable while existing imports continue to use
``skcoord.discovery``.
"""

from .discovery_assets import (
    _walk_filesystems,
    collect_datastores,
    collect_model_endpoints,
    collect_observed_agents,
)
from .discovery_base import (
    AUTHORITY_DECLARED,
    AUTHORITY_OBSERVED,
    DISCOVERED_TAG,
    OBSERVATION_SCHEMA_VERSION,
    CommandRunner,
    DiscoveredCI,
    LocalRunner,
    ObservationState,
    SSHRunner,
    _exec,
    _normalise_alias,
    _utc_now,
    ci_observation_state,
    observation_state,
)
from .discovery_declared import (
    _read_json,
    collect_agents,
    collect_fleet_objects,
    collect_registry,
    device_from_fingerprint,
)
from .discovery_drift import DriftFinding, _service_key, drift
from .discovery_identity import (
    IDENTITY_EVIDENCE_MAX_AGE,
    collect_identity_estate,
    identity_estate_drift,
)
from .discovery_reconcile import (
    ReconcileReport,
    _observed_status,
    _redact_attributes,
    _validated_discovery,
    reconcile,
)
from .discovery_runtime import (
    _SS_RE,
    _cron_rows,
    _ephemeral_range,
    collect_cron_jobs,
    collect_host_facts,
    collect_listening_ports,
    collect_network_interfaces,
)
from .discovery_scan import (
    DECLARED_COLLECTORS,
    OBSERVED_COLLECTORS,
    _matching_identity_ids,
    merge,
    scan,
)
from .discovery_systemd import (
    ORIGIN_DISTRO,
    ORIGIN_OPERATOR,
    ORIGIN_UNKNOWN,
    _classify_origin,
    _fragment_paths,
    _unit_dependencies,
    collect_docker_containers,
    collect_systemd_units,
)

__all__ = [
    "_SS_RE",
    "_classify_origin",
    "_cron_rows",
    "_ephemeral_range",
    "_exec",
    "_fragment_paths",
    "_matching_identity_ids",
    "_normalise_alias",
    "_observed_status",
    "_read_json",
    "_redact_attributes",
    "_service_key",
    "_unit_dependencies",
    "_utc_now",
    "_validated_discovery",
    "_walk_filesystems",
    "AUTHORITY_DECLARED",
    "AUTHORITY_OBSERVED",
    "DECLARED_COLLECTORS",
    "DISCOVERED_TAG",
    "IDENTITY_EVIDENCE_MAX_AGE",
    "OBSERVATION_SCHEMA_VERSION",
    "OBSERVED_COLLECTORS",
    "ORIGIN_DISTRO",
    "ORIGIN_OPERATOR",
    "ORIGIN_UNKNOWN",
    "CommandRunner",
    "DiscoveredCI",
    "DriftFinding",
    "LocalRunner",
    "ObservationState",
    "ReconcileReport",
    "SSHRunner",
    "ci_observation_state",
    "collect_agents",
    "collect_cron_jobs",
    "collect_datastores",
    "collect_docker_containers",
    "collect_fleet_objects",
    "collect_host_facts",
    "collect_identity_estate",
    "collect_listening_ports",
    "collect_model_endpoints",
    "collect_network_interfaces",
    "collect_observed_agents",
    "collect_registry",
    "collect_systemd_units",
    "device_from_fingerprint",
    "drift",
    "identity_estate_drift",
    "merge",
    "observation_state",
    "reconcile",
    "scan",
]
