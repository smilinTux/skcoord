"""Tests for connects_to edge derivation (card 6e010c63, epic 122ebff1 AC3).

Two collectors, both read-only and fixture-driven through FakeRunner:

* collect_port_service_links: ss listeners -> cgroup -> unit/container ->
  connects_to edge on the owning service CI. Unattributable listeners must
  produce NO edge; a guessed topology edge is worse than none.
* collect_container_networks: docker/podman network inspect -> NETWORK CIs
  plus container membership edges. Malformed inspect JSON is skipped, a
  missing runtime is explicit partial coverage, and nothing crashy happens.

The sanitizer contract (PR #25) is pinned too: no emitted CI may carry an
attribute key the CMDB would reject as secret-looking.
"""

from __future__ import annotations

import json

from skcoord.cmdb import CIType, is_secret_attribute_key, make_ci_id
from skcoord.discovery_connects import (
    collect_container_networks,
    collect_port_service_links,
)

SS_WITH_PROCS = """\
LISTEN 0      4096                                  0.0.0.0:8384        0.0.0.0:*    users:(("syncthing",pid=1234,fd=46))
LISTEN 0      128                                         *:22              *:*    users:(("sshd",pid=900,fd=3))
LISTEN 0      511                                   0.0.0.0:11434       0.0.0.0:*    users:(("ollama",pid=2000,fd=12))
LISTEN 0      100                                   0.0.0.0:35777       0.0.0.0:*    users:(("mystery",pid=3000,fd=9))
"""

SS_NO_PROCS = """\
LISTEN 0      4096                                  0.0.0.0:8384        0.0.0.0:*
"""

SYSTEMD_UNITS = """\
syncthing.service  loaded active running Syncthing
sshd.service       loaded active running OpenSSH daemon
ollama.service     loaded active running Ollama
"""

DOCKER_PS_IDS = "a1b2c3d4e5f6\tollama\nf6e5d4c3b2a1\tredis\n"

OLLAMA_CGROUP = (
    "0::/system.slice/docker-"
    "a1b2c3d4e5f6" + "0" * 50 + "de" + ".scope\n"
)

ANSWERS = {
    "ss -tlnpH": SS_WITH_PROCS,
    "ss -tlnH": None,
    "ip_local_port_range": "32768 60999",
    "/proc/1234/cgroup": "0::/system.slice/syncthing.service\n",
    "/proc/900/cgroup": "0::/user.slice/user-1000.slice/user@1000.service/app.slice/sshd.service\n",
    "/proc/2000/cgroup": OLLAMA_CGROUP,
    "/proc/3000/cgroup": "0::/user.slice/user-1000.slice/session-3.scope\n",
    "list-units": SYSTEMD_UNITS,
    "{{.ID}}": DOCKER_PS_IDS,
}


class FakeRunner:
    """A runner with canned answers, so collectors are testable off-fleet."""

    def __init__(self, host: str = "testnode", answers: dict | None = None) -> None:
        self.host = host
        self.answers = answers or {}
        self.calls: list[list[str]] = []

    def run(self, argv):
        self.calls.append(list(argv))
        for key, value in self.answers.items():
            if key in " ".join(argv):
                return value
        return None


def _connects(ci) -> set[str]:
    return {target for rel_type, target in ci.relationships if rel_type == "connects_to"}


def _by_name(cis):
    return {ci.name: ci for ci in cis}


# --------------------------------------------------------- port attribution ---


def test_systemd_and_container_owners_get_edges() -> None:
    runner = FakeRunner(answers=dict(ANSWERS))
    cis = _by_name(collect_port_service_links(runner))

    syncthing_port = make_ci_id(CIType.PORT.value, "testnode:8384")
    sshd_port = make_ci_id(CIType.PORT.value, "testnode:22")
    ollama_port = make_ci_id(CIType.PORT.value, "testnode:11434")

    assert _connects(cis["syncthing"]) == {syncthing_port}
    # The user-slice wrapper (user@1000.service) is skipped; the inner unit wins.
    assert _connects(cis["sshd"]) == {sshd_port}
    # The docker scope id prefix-matches the docker ps short id: the edge is
    # attributed to the container name, not to a hex id.
    assert _connects(cis["ollama"]) == {ollama_port}


def test_unattributable_listener_gets_no_edge() -> None:
    runner = FakeRunner(answers=dict(ANSWERS))
    cis = collect_port_service_links(runner)
    names = {ci.name for ci in cis}
    # session-3.scope is neither a service nor a container: no edge, no CI.
    assert "mystery" not in names
    assert all("35777" not in target for ci in cis for _, target in ci.relationships)


def test_unit_not_loaded_gets_no_edge() -> None:
    answers = dict(ANSWERS)
    answers["list-units"] = "sshd.service loaded active running OpenSSH\n"
    runner = FakeRunner(answers=answers)
    cis = _by_name(collect_port_service_links(runner))
    # syncthing is not in the loaded-unit list: no edge to a CI that does not exist.
    assert "syncthing" not in cis
    assert "sshd" in cis


def test_missing_ss_p_permission_is_partial_coverage_not_crash() -> None:
    answers = dict(ANSWERS)
    answers["ss -tlnpH"] = None  # -p denied
    answers["ss -tlnH"] = SS_NO_PROCS
    runner = FakeRunner(answers=answers)
    assert collect_port_service_links(runner) == []


def test_ss_entirely_unavailable_returns_nothing() -> None:
    runner = FakeRunner(answers={"ip_local_port_range": "32768 60999"})
    assert collect_port_service_links(runner) == []


def test_ephemeral_ports_never_get_edges() -> None:
    answers = dict(ANSWERS)
    answers["ss -tlnpH"] = (
        'LISTEN 0 100 0.0.0.0:45000 0.0.0.0:* users:(("syncthing",pid=1234,fd=46))\n'
    )
    runner = FakeRunner(answers=answers)
    assert collect_port_service_links(runner) == []


# ------------------------------------------------------- container networks ---

NETWORK_INSPECT = json.dumps(
    [
        {
            "Name": "bridge",
            "Driver": "bridge",
            "Scope": "local",
            "Containers": {
                "a1b2": {"Name": "ollama"},
                "c3d4": {"Name": "redis"},
            },
        },
        {
            "Name": "appnet",
            "Driver": "bridge",
            "Scope": "local",
            "Containers": {"e5f6": {"Name": "ollama"}},
        },
    ]
)


def test_network_membership_edges_and_network_cis() -> None:
    runner = FakeRunner(
        answers={
            "network ls": "bridge\nappnet\n",
            "network inspect": NETWORK_INSPECT,
        }
    )
    cis = collect_container_networks(runner)
    by_name = _by_name(cis)

    bridge_id = make_ci_id(CIType.NETWORK.value, "testnode:bridge")
    appnet_id = make_ci_id(CIType.NETWORK.value, "testnode:appnet")

    # The default bridge is edged like any other network (documented call).
    assert _connects(by_name["ollama"]) == {bridge_id, appnet_id}
    assert _connects(by_name["redis"]) == {bridge_id}

    bridge_ci = by_name["testnode:bridge"]
    assert bridge_ci.ci_type == CIType.NETWORK.value
    assert bridge_ci.attributes["driver"] == "bridge"
    assert bridge_ci.attributes["network_name"] == "bridge"


def test_network_inspect_malformed_json_is_skipped() -> None:
    runner = FakeRunner(
        answers={"network ls": "bridge\n", "network inspect": "{not json"}
    )
    assert collect_container_networks(runner) == []


def test_network_inspect_non_list_is_skipped() -> None:
    runner = FakeRunner(
        answers={"network ls": "bridge\n", "network inspect": '{"Name": "bridge"}'}
    )
    assert collect_container_networks(runner) == []


def test_no_container_runtime_is_partial_coverage_not_crash() -> None:
    runner = FakeRunner(answers={})
    assert collect_container_networks(runner) == []


def test_podman_lowercase_inspect_shape_is_accepted() -> None:
    document = json.dumps(
        [
            {
                "name": "podnet",
                "driver": "bridge",
                "containers": {"ab12": {"name": "redis"}},
            }
        ]
    )
    runner = FakeRunner(
        answers={
            "podman network ls": "podnet\n",
            "podman network inspect": document,
        }
    )
    cis = _by_name(collect_container_networks(runner))
    podnet_id = make_ci_id(CIType.NETWORK.value, "testnode:podnet")
    assert "testnode:podnet" in cis
    assert _connects(cis["redis"]) == {podnet_id}


# ------------------------------------------------------------- secret hygiene ---


def test_no_emitted_attribute_key_looks_secret() -> None:
    runner = FakeRunner(answers=dict(ANSWERS))
    cis = list(collect_port_service_links(runner))
    runner = FakeRunner(
        answers={"network ls": "bridge\nappnet\n", "network inspect": NETWORK_INSPECT}
    )
    cis += collect_container_networks(runner)
    assert cis, "fixture must produce CIs"
    for ci in cis:
        for key in ci.attributes:
            assert not is_secret_attribute_key(key), f"{ci.name}: {key}"


# ------------------------------------------------------- pipeline integration ---


def test_scan_folds_connects_edges_onto_service_cis(tmp_path) -> None:
    """scan() must run the connects collectors and merge their edges.

    The systemd sighting of syncthing (runs_on) and the port-attribution
    sighting (connects_to) are the same CI after the scan merge; the network
    CI from network inspect appears as its own record.
    """
    from skcoord.discovery import scan

    runner = FakeRunner(answers=dict(ANSWERS))
    cis = _by_name(scan(tmp_path, runners=[runner], include_declared=False))

    syncthing = cis["syncthing"]
    rel_types = {rel_type for rel_type, _ in syncthing.relationships}
    assert "runs_on" in rel_types
    assert "connects_to" in rel_types
    assert make_ci_id(CIType.PORT.value, "testnode:8384") in _connects(syncthing)


def test_scan_surfaces_network_cis(tmp_path) -> None:
    from skcoord.discovery import scan

    runner = FakeRunner(
        answers={"network ls": "bridge\nappnet\n", "network inspect": NETWORK_INSPECT}
    )
    cis = _by_name(scan(tmp_path, runners=[runner], include_declared=False))
    assert "testnode:appnet" in cis
    assert cis["testnode:appnet"].ci_type == CIType.NETWORK.value
    assert _connects(cis["ollama"]) == {
        make_ci_id(CIType.NETWORK.value, "testnode:bridge"),
        make_ci_id(CIType.NETWORK.value, "testnode:appnet"),
    }
