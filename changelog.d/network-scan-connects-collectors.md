### Fixed

- `scan_network` runs the same per-host collectors as a local scan, adding `CONNECTS_COLLECTORS` (port ownership and container network membership). A network pass no longer reports every stored docker network and port-to-service edge as `stored_not_discovered`, and a governed network apply would not orphan them. The network scope fingerprint changes once (collector list), so record network shadows after this lands.
