"""Shared discovery models, evidence state, and command runners."""

import logging
import shlex
import subprocess
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional, Protocol, Sequence

from .cmdb import (
    make_ci_id,
)

logger = logging.getLogger("skcoord.discovery")

DISCOVERED_TAG = "discovered"
"""Marks a CI as discovery-owned, so orphan handling never touches hand-made CIs."""

_DEFAULT_TIMEOUT = 20

AUTHORITY_DECLARED = "declared"
AUTHORITY_OBSERVED = "observed"
OBSERVATION_SCHEMA_VERSION = 1


class ObservationState(str, Enum):
    """Evidence quality, deliberately independent from CI health."""

    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


def observation_state(
    observed_at: str, *, now: Optional[datetime] = None, max_age: timedelta = timedelta(hours=6)
) -> ObservationState:
    if not observed_at:
        return ObservationState.UNKNOWN
    try:
        timestamp = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError:
        return ObservationState.UNKNOWN
    current = now or datetime.now(timezone.utc)
    return ObservationState.FRESH if current - timestamp <= max_age else ObservationState.STALE


def ci_observation_state(
    ci: object, *, now: Optional[datetime] = None, max_age: timedelta = timedelta(hours=6)
) -> ObservationState:
    """Derive current evidence freshness from a folded CI's timestamp.

    Freshness is intentionally never trusted from a stored ``observation_state``
    attribute: elapsed wall time can make that value false without a new event.
    """
    attributes = getattr(ci, "attributes", {})
    observed_at = attributes.get("observed_at", "") if isinstance(attributes, dict) else ""
    return observation_state(str(observed_at), now=now, max_age=max_age)


# ---------------------------------------------------------------------------
# The unit of discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoveredCI:
    """One configuration item as some collector saw it.

    ``observed`` is the whole point: True means a machine was asked and
    answered, False means a file claimed it. Merging never lets a declaration
    overwrite an observation.
    """

    ci_type: str
    name: str
    source: str
    observed: bool = False
    node: str = ""
    description: str = ""
    attributes: dict = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    relationships: tuple[tuple[str, str], ...] = ()
    declared_flag: Optional[bool] = None
    canonical_name: str = ""
    aliases: tuple[str, ...] = ()
    observed_at: str = ""
    scan_id: str = ""
    authority: str = ""
    lifecycle_scope: str = ""
    schema_version: int = OBSERVATION_SCHEMA_VERSION

    @property
    def ci_id(self) -> str:
        return make_ci_id(self.ci_type, self.canonical_name or self.name)

    @property
    def declared(self) -> bool:
        """Whether a spec claims this CI exists.

        Kept separate from ``observed`` because the healthy case is *both*, and
        a merge that collapsed them into one flag made every running,
        correctly-declared service look undocumented.
        """
        return (not self.observed) if self.declared_flag is None else self.declared_flag

    def merged_with(self, other: "DiscoveredCI") -> "DiscoveredCI":
        """Fold another sighting of the same CI into this one.

        Observations win over declarations for the scalar fields; attributes
        and tags union, with the observed side taking precedence on conflict.
        Both provenance flags survive: the result is declared *and* observed.
        """
        primary, secondary = (
            (other, self) if (other.observed and not self.observed) else (self, other)
        )
        if self.declared and not other.declared:
            canonical_name = self.canonical_name or self.name
        elif other.declared and not self.declared:
            canonical_name = other.canonical_name or other.name
        else:
            canonical_name = min(
                filter(
                    None, (self.canonical_name or self.name, other.canonical_name or other.name)
                )
            )
        attributes = {**secondary.attributes, **primary.attributes}
        sources = sorted({*self.source.split("+"), *other.source.split("+")})
        return replace(
            primary,
            source="+".join(sources),
            observed=self.observed or other.observed,
            declared_flag=self.declared or other.declared,
            node=primary.node or secondary.node,
            description=primary.description or secondary.description,
            attributes=attributes,
            tags=tuple(sorted({*self.tags, *other.tags})),
            relationships=tuple(sorted({*self.relationships, *other.relationships})),
            canonical_name=canonical_name,
            aliases=tuple(sorted({*self.identity_aliases, *other.identity_aliases})),
            observed_at=max(self.observed_at, other.observed_at),
            scan_id=primary.scan_id or secondary.scan_id,
            authority=(
                AUTHORITY_OBSERVED if self.observed or other.observed else AUTHORITY_DECLARED
            ),
            lifecycle_scope=primary.lifecycle_scope or secondary.lifecycle_scope,
        )

    @property
    def identity_aliases(self) -> tuple[str, ...]:
        """Normalised names by which this CI may be recognised."""
        values = {self.name, self.canonical_name, *self.aliases}
        return tuple(sorted({_normalise_alias(v) for v in values if v}))


def _normalise_alias(value: str) -> str:
    return str(value).strip().lower().rstrip(".")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Machine access
# ---------------------------------------------------------------------------


class CommandRunner(Protocol):
    """Runs a command somewhere and returns stdout, or None if it failed."""

    host: str

    def run(self, argv: Sequence[str]) -> Optional[str]: ...


@dataclass
class LocalRunner:
    """Runs commands on this machine."""

    host: str = ""
    timeout: int = _DEFAULT_TIMEOUT

    def __post_init__(self) -> None:
        if not self.host:
            import socket

            self.host = socket.gethostname()

    def run(self, argv: Sequence[str]) -> Optional[str]:
        return _exec(list(argv), self.timeout)


@dataclass
class SSHRunner:
    """Runs commands on another node over ssh.

    Used for the chi fleet and any node that is reachable but not part of this
    coordination home. BatchMode keeps a missing key a fast failure instead of
    a password prompt that hangs a cron job.
    """

    host: str
    target: str = ""
    timeout: int = _DEFAULT_TIMEOUT

    def run(self, argv: Sequence[str]) -> Optional[str]:
        remote = " ".join(shlex.quote(a) for a in argv)
        ssh = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={min(self.timeout, 10)}",
            self.target or self.host,
            remote,
        ]
        return _exec(ssh, self.timeout)


def _exec(argv: list[str], timeout: int) -> Optional[str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("command failed: %s (%s)", argv[0], exc)
        return None
    if proc.returncode != 0:
        logger.debug("command rc=%s: %s", proc.returncode, argv)
        return None
    return proc.stdout


# ---------------------------------------------------------------------------
# Declared-state collectors (read specs, no machine access)
# ---------------------------------------------------------------------------
