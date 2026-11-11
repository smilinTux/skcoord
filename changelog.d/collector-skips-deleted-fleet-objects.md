### Fixed

- Discovery no longer treats a fleet Service, CronJob or Operatorapp with `spec.deleted: true` as a declaration, matching the fleet controllers. A retired object used to stay `declared_not_observed` forever.
