### Fixed

- Network reconcile artifacts carry `scope: "network"` and `drift.by_kind`, the fields local artifacts already carry and SKBrain CmdbDriftBounded reads (card d3aa9751). A recorded network shadow no longer reads as "no drift summary, scope unscoped". The scope fingerprint is unchanged.
