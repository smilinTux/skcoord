"""Validation for explicit, owner-requested isolated source helpers."""

from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from .card_store import explicit_creation_request_digest

if TYPE_CHECKING:
    from .card_store import CardStore
    from .coordination import Task

_SOURCE = ("repository", "base_ref", "base_revision")
_HELPER = (
    "helper_request_id",
    "helper_parent_id",
    "helper_parent_claim_revision",
    "helper_objective",
    "helper_allowed_paths",
    "helper_verification",
    "helper_parent_contract_sha256",
    "logical_route",
)

_GENERIC_ROUTES = ("sk-s", "sk-m", "sk-l", "sk-xl")
_FOCUSED_ROUTES = tuple(
    f"sk-{family}-{size}"
    for family in ("codex", "deepseek", "glm", "zai")
    for size in ("s", "m", "l")
)
_ROUTES = _GENERIC_ROUTES + _FOCUSED_ROUTES
_ROUTE_LABEL = re.compile(r"sk-[a-z]+(?:-[a-z]+)?")


def _is_route_label(label: str) -> bool:
    """Reserve route-shaped labels, including unsupported route spellings."""
    return _ROUTE_LABEL.fullmatch(label.strip().lower()) is not None


def parent_contract_digest(parent) -> str:
    """Bind inherited instructions which can change without a new claim."""
    return explicit_creation_request_digest(
        {
            "title": parent.title,
            "description": parent.description,
            "acceptance_criteria": list(parent.acceptance_criteria),
        }
    )


def _strings(value: object, name: str, *, empty: bool = False) -> list[str]:
    """Reject coerced, blank or oversized lists at the request boundary."""
    if not isinstance(value, list) or (not value and not empty) or len(value) > 64:
        raise ValueError(f"{name} must be a bounded list")
    if any(not isinstance(item, str) or not item.strip() or len(item) > 4096 for item in value):
        raise ValueError(f"{name} must contain nonempty strings")
    if len(value) != len(set(value)):
        raise ValueError(f"{name} contains duplicates")
    return value


def _paths(value: object) -> list[PurePosixPath]:
    """Require literal repository paths so overlap checks are unambiguous."""
    paths = _strings(value, "helper_allowed_paths", empty=True)
    result = []
    for value in paths:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or str(path) != value
            or value == "."
            or any(part in {"..", ".git"} for part in path.parts)
            or any(char in value for char in "\\\x00*?[]")
        ):
            raise ValueError("helper_allowed_paths must be literal repository paths")
        result.append(path)
    return result


def _binding(parent) -> dict[str, str]:
    """Read exact source without silently resolving conflicting projections."""
    result = {}
    for key in _SOURCE:
        values = []
        for mapping in (parent.meta, parent.links):
            value = mapping.get(key)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"parent {key} must be a string")
            if isinstance(value, str) and value.strip():
                value = value.strip()
                values.append(value.lower() if key == "base_revision" else value)
        if not values or len(set(values)) != 1:
            raise ValueError(f"parent {key} is missing or conflicting")
        result[key] = values[0]
    url = urlsplit(result["repository"])
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("parent repository must be credential-free HTTPS")
    if not re.fullmatch(r"[0-9a-f]{40}", result["base_revision"]):
        raise ValueError("parent base_revision must be an exact commit")
    if any(char.isspace() or ord(char) < 32 for char in result["base_ref"]):
        raise ValueError("parent base_ref must be a named ref")
    return result


def validate_helper_request(
    store: CardStore,
    task: Task,
    digest: str,
    actor: str,
    parent_id: str,
    expected_claim_revision: str,
) -> None:
    """Validate under board/parent locks, before any child becomes visible."""
    parent = store.fold(parent_id)
    if (
        parent is None
        or parent.archived
        or parent.meta.get("voided")
        or parent.status.value not in {"ready", "doing"}
        or parent.owner != actor
        or not actor
        or not expected_claim_revision
        or parent.meta.get("_claim_revision") != expected_claim_revision
        or parent.meta.get("claim_conflicts")
    ):
        raise ValueError("helper parent owner/claim is not current and active")
    if (
        parent.kind.value != "task"
        or parent.meta.get("helper_parent_id")
        or any(
            label.lower().startswith("seat-") or label.lower() == "review"
            for label in parent.labels
        )
        or re.search(r"\[(?:REVIEW|REREVIEW)\]", parent.title, re.I)
    ):
        raise ValueError("governed reviewers and helpers cannot delegate helpers")
    if task.created_by != actor or task.created_at != parent.created_at:
        raise ValueError("helper creator and stable creation time must match its parent")
    if set(task.meta) != set(_SOURCE + _HELPER):
        raise ValueError("helper metadata must contain only source and helper contract fields")
    if task.meta["helper_parent_contract_sha256"] != parent_contract_digest(parent):
        raise ValueError("helper parent contract changed before creation")
    request_id = task.meta["helper_request_id"]
    if not isinstance(request_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", request_id
    ):
        raise ValueError("helper_request_id must be a stable request identifier")
    expected_id = hashlib.sha256(f"{parent_id}:{request_id}".encode()).hexdigest()[:8]
    if task.id != expected_id:
        raise ValueError("helper ID must bind its parent and request")
    if (
        task.meta["helper_parent_id"] != parent_id
        or task.meta["helper_parent_claim_revision"] != expected_claim_revision
    ):
        raise ValueError("helper metadata does not match the parent's current claim")
    for key, value in _binding(parent).items():
        if not isinstance(task.meta[key], str) or task.meta[key] != value:
            raise ValueError(f"helper {key} must match its parent's exact source")
    route = task.meta["logical_route"]
    if not isinstance(route, str) or route not in _ROUTES:
        raise ValueError("helper logical_route is not a supported exact route")
    if any(
        _is_route_label(label) and label.strip().lower() not in _ROUTES for label in parent.labels
    ):
        raise ValueError("helper parent has an unsupported inherited route")
    # Prefixes such as "parent-" or "inherited-" mark unrelated card-scope
    # or legacy labels and must be preserved byte-for-byte; inherited route
    # labels are reserved by shape; unknown inherited routes fail above.
    # Supported route spellings match the F3 producer, so padded, case-varied, and
    # tab-suffixed legacy route labels are removed and replaced with the
    # metadata-selected route tag.
    route_count = sum(
        1 for label in task.tags if _is_route_label(label) and label.strip().lower() == route
    )
    if route_count != 1:
        raise ValueError("helper must carry exactly one occurrence of the selected route tag")
    other_route_count = sum(
        1 for label in task.tags if _is_route_label(label) and label.strip().lower() != route
    )
    if other_route_count:
        raise ValueError("helper must not carry a second exact route tag")
    kept = {
        label
        for label in parent.labels
        if label.startswith("parent-") or not _is_route_label(label)
    }
    required = kept | {"source-only", "owner-helper", f"parent-{parent_id}", route}
    if set(task.tags) != required:
        raise ValueError("helper must preserve parent restrictions without granting new ones")
    if (
        set(task.dependencies) != set(parent.dependencies)
        or parent_id in task.dependencies
        or task.id in task.dependencies
    ):
        raise ValueError("helper must retain external dependencies without a parent cycle")
    if re.findall(r"\[(?:S|M|L|XL)\]", task.title) not in [
        ["[S]"],
        ["[M]"],
    ] or re.search(r"\[(?:REVIEW|REREVIEW)\]", task.title, re.I):
        raise ValueError("helper needs a bounded source title")
    _strings(task.acceptance_criteria, "acceptance_criteria")
    _strings(task.meta["helper_verification"], "helper_verification")
    objective = task.meta["helper_objective"]
    if not isinstance(objective, str) or not objective.strip() or len(objective) > 8192:
        raise ValueError("helper_objective must identify a bounded output")
    paths = _paths(task.meta["helper_allowed_paths"])
    if digest != explicit_creation_request_digest(task.model_dump(mode="json")):
        raise ValueError("helper request digest does not match its exact content")
    if not paths:
        return
    # Creation is infrequent. Use authoritative folds, never stale worker views.
    for other in store.list_cards():
        if (
            other.id == task.id
            or other.archived
            or other.meta.get("voided")
            or other.status.value == "done"
            or other.meta.get("helper_parent_id") != parent_id
        ):
            continue
        other_paths = _paths(other.meta.get("helper_allowed_paths"))
        if any(a == b or a in b.parents or b in a.parents for a in paths for b in other_paths):
            raise ValueError(f"helper write paths overlap active helper {other.id}")
