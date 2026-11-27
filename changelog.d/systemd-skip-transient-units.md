### Fixed

- The systemd collector skips transient units (fragment under `/systemd/transient/`: D-Bus activation and `systemd-run`). Their per-session names were reported as operator `observed_not_declared` services and minted new CIs every login.
