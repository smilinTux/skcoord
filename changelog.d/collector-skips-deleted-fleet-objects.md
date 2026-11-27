### Fixed

- Discovery no longer treats a fleet Service, CronJob or Operatorapp with `spec.deleted: true` as a declaration, matching the fleet controllers. A retired object used to stay `declared_not_observed` forever.
- A registry entry's `systemd_unit` (recorded by skcapstone `register_service`) is a match alias, so a service registered under one name and running as another unit is no longer `declared_not_observed`.
