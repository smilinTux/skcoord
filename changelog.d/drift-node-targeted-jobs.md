### Fixed

- Drift: a fleet CronJob whose command is `systemctl [--user] start <unit>` now matches that unit, and declarations record `target_nodes` (placement, fleet status, or nodeSelector against Node labels). drift() counts a targeted declaration missing only when its node was scanned, and matches it only against units observed on that node, so per-node jobs for other nodes no longer appear as `declared_not_observed` in a local scan.
- Crontab discovery recognises the installed `sk-cron-run` console script as well as `sk-cron-run.sh`, so wrapped entries carry their job-name alias. Fingerprints are unchanged, so no CI identity churn.
