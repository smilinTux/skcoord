### Fixed

- Ignore Syncthing `*.sync-conflict-*` copies in `load_agents` and `load_tasks` instead of warning "agent payload identity does not match its filename" once per copy per call. Each copy is counted once per process at debug level. The overlay "N unreadable lines total" summary is also logged once per process per (file, count) instead of once per fold.
