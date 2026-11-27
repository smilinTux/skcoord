### Fixed

- `collect_cron_jobs` runs a fixed presence probe first, so a node with no cron at all (no `crontab`, no `/etc/crontab`) reports partial coverage instead of an unavailable collector. Such a node no longer leaves every network scan that includes it incomplete.
