### Fixed

- A coordination `Task` now carries `exit_gates`, `non_goals` and
  `spec_version`, and the CardStore mirror (`mirror_coord_create`,
  `mirror_coord_create_explicit`, `mirror_coord_create_claimed`) writes them
  into the card core instead of dropping them. Tasks that set none of them
  mirror exactly as before.
