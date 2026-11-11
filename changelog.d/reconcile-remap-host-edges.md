### Fixed

- Reconcile rewrites relationship targets when a host or device sighting
  resolves to an existing CI under another id. Before, every `runs_on` edge
  kept the sighting's own id and dangled (chg-a543c87b: 701 edges on
  `ci-host-192.168.0.41` and `ci-host-192.168.0.100`). A rerun removes the
  dangling edges as stale and adds the correct ones.
