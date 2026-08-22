"""Observed datastore, agent, and model endpoint collectors."""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

from .cmdb import CIType, make_ci_id
from .discovery_base import DISCOVERED_TAG, CommandRunner, DiscoveredCI
from .discovery_runtime import _PERSISTENT_FILESYSTEMS, _PSEUDO_FILESYSTEMS


def _walk_filesystems(rows: object) -> Iterable[dict]:
    """Flatten findmnt's recursive JSON tree without trusting child shape."""
    if not isinstance(rows, list):
        return
    for row in rows:
        if not isinstance(row, dict):
            continue
        yield row
        yield from _walk_filesystems(row.get("children"))


def collect_datastores(runner: CommandRunner) -> list[DiscoveredCI]:
    """Persistent filesystem mounts and database-like containers as datastores."""
    out: list[DiscoveredCI] = []
    stdout = runner.run(
        ["findmnt", "-J", "-b", "-o", "TARGET,SOURCE,FSTYPE,SIZE,USED,AVAIL,USE%"]
    )
    if stdout:
        try:
            filesystems = json.loads(stdout).get("filesystems", [])
        except (TypeError, ValueError):
            filesystems = []
        for filesystem in _walk_filesystems(filesystems):
            target = str(filesystem.get("target", "")).strip()
            fstype = str(filesystem.get("fstype", "")).strip().lower()
            if (
                not target
                or fstype in _PSEUDO_FILESYSTEMS
                or fstype not in _PERSISTENT_FILESYSTEMS
            ):
                continue
            attributes = {
                key: filesystem[key]
                for key in ("source", "fstype", "size", "used", "avail", "use%")
                if filesystem.get(key) not in (None, "")
            }
            attributes["mountpoint"] = target
            out.append(
                DiscoveredCI(
                    ci_type=CIType.DATASTORE.value,
                    name=f"{runner.host}:mount:{target}",
                    source="findmnt",
                    observed=True,
                    node=runner.host,
                    attributes=attributes,
                    tags=("mount", "datastore", DISCOVERED_TAG),
                    relationships=(("runs_on", make_ci_id(CIType.HOST.value, runner.host)),),
                )
            )

    database_hint = re.compile(
        r"(?:postgres|skmem-pg|mysql|mariadb|mongo|redis|qdrant|weaviate|neo4j|falkor)",
        re.IGNORECASE,
    )
    seen: set[str] = set()
    for runtime in ("docker", "podman"):
        containers = runner.run([runtime, "ps", "--format", "{{.Names}}\t{{.Image}}"])
        if not containers:
            continue
        for line in containers.splitlines():
            name, _, image = line.partition("\t")
            name = name.strip()
            image = image.strip()
            if not name or name in seen or not database_hint.search(f"{name} {image}"):
                continue
            seen.add(name)
            out.append(
                DiscoveredCI(
                    ci_type=CIType.DATASTORE.value,
                    name=f"{runner.host}:container:{name}",
                    source=f"{runtime}:datastore",
                    observed=True,
                    node=runner.host,
                    attributes={"runtime": runtime, "container": name, "image": image},
                    tags=(runtime, "database", "datastore", DISCOVERED_TAG),
                    relationships=(("runs_on", make_ci_id(CIType.HOST.value, runner.host)),),
                )
            )
    return out


_AGENT_LIST_SCRIPT = """import json
from pathlib import Path
p = Path.home() / '.skcapstone' / 'agents'
print(json.dumps(sorted(x.name for x in p.iterdir() if x.is_dir()))) if p.is_dir() else print('[]')
"""


def collect_observed_agents(runner: CommandRunner) -> list[DiscoveredCI]:
    """Agent homes observed on the target rather than declared on the scanner."""
    stdout = runner.run(["python3", "-c", _AGENT_LIST_SCRIPT])
    if not stdout:
        return []
    try:
        names = json.loads(stdout)
    except (TypeError, ValueError):
        return []
    if not isinstance(names, list):
        return []
    return [
        DiscoveredCI(
            ci_type=CIType.AGENT.value,
            name=str(name),
            source="agent-home",
            observed=True,
            node=runner.host,
            attributes={"home_present": True},
            tags=("agent", DISCOVERED_TAG),
            relationships=(("runs_on", make_ci_id(CIType.HOST.value, runner.host)),),
        )
        for name in names
        if str(name).strip() and not str(name).endswith("-template")
    ]


def collect_model_endpoints(runner: CommandRunner) -> list[DiscoveredCI]:
    """Observed Ollama endpoint, version and bounded installed-model inventory."""
    models = runner.run(["ollama", "list"])
    version = runner.run(
        [
            "python3",
            "-c",
            "import urllib.request; "
            "u='http://127.0.0.1:11434/api/version'; "
            "\ntry: print(urllib.request.urlopen(u, timeout=2).read().decode())"
            "\nexcept Exception: print('{}')",
        ]
    )
    parsed: dict[str, Any] = {}
    if version:
        try:
            candidate = json.loads(version)
            if isinstance(candidate, dict):
                parsed = candidate
        except (TypeError, ValueError):
            pass
    if not models and not parsed.get("version"):
        return []
    attributes: dict[str, Any] = {
        "endpoint": "http://127.0.0.1:11434",
        "health_observed": bool(parsed.get("version")),
    }
    if parsed.get("version"):
        attributes["version"] = parsed["version"]
    if models:
        names = [line.split()[0] for line in models.splitlines()[1:] if line.split()]
        attributes["models"] = sorted(set(names))[:100]
        attributes["model_count"] = len(set(names))
        attributes["models_truncated"] = len(set(names)) > 100
    return [
        DiscoveredCI(
            ci_type=CIType.SERVICE.value,
            name=f"{runner.host}:ollama",
            source="ollama",
            observed=True,
            node=runner.host,
            attributes=attributes,
            tags=("model-api", "ollama", DISCOVERED_TAG),
            relationships=(("runs_on", make_ci_id(CIType.HOST.value, runner.host)),),
        )
    ]

