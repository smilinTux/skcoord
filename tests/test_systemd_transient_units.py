"""Transient systemd units are not assets.

D-Bus activation and ``systemd-run`` create units under
``/run/user/<uid>/systemd/transient/`` (or ``/run/systemd/transient/``) with
per-session names such as ``dbus-:1.1-org.cinnamon.ScreenSaver@7``. A network
scan of .41 on 2026-11-11 reported them as operator-authored
``observed_not_declared`` services, and each login minted new CI names, so
the previous session's ones piled up as ``stored_not_discovered``.
"""

from __future__ import annotations

from skcoord.discovery import collect_systemd_units

UNITS = """skgateway.service                                  loaded active running SKGateway
dbus-:1.1-org.cinnamon.ScreenSaver@7.service       loaded active running ScreenSaver
run-u42.service                                    loaded active running /usr/bin/true
"""

PATHS = """Id=skgateway.service
FragmentPath=/home/u/.config/systemd/user/skgateway.service

Id=dbus-:1.1-org.cinnamon.ScreenSaver@7.service
FragmentPath=/run/user/1000/systemd/transient/dbus-:1.1-org.cinnamon.ScreenSaver@7.service

Id=run-u42.service
FragmentPath=/run/systemd/transient/run-u42.service
"""


class FakeRunner:
    host = "laptop"

    def run(self, argv):
        cmd = " ".join(argv)
        if "list-units" in cmd and "--type=service" in cmd and "--user" in cmd:
            return UNITS
        if "FragmentPath" in cmd and "--user" in cmd:
            return PATHS
        return ""


def test_transient_units_are_not_collected() -> None:
    names = {ci.name for ci in collect_systemd_units(FakeRunner(), scopes=("--user",))}
    assert names == {"skgateway"}
