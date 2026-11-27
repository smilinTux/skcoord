### Fixed

- Drift: a unit seen running on several nodes, which merge() folds into one CI with a single `node`, now counts as observed on every node its `runs_on` edges name. A node-targeted declaration (for example a control-plane Service backed by `claude-code-api.service`, which runs on three nodes) no longer reads as `declared_not_observed` in a network scan because the folded CI kept another node.
