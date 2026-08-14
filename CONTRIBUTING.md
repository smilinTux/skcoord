# Contributing to skcoord

Thanks for working on the coordination core. This guide covers the ground rules,
the branch model, the commit convention, the test gate, and the review path. It
complements [SOP.md](./SOP.md) and [SECURITY.md](./SECURITY.md).

---

## Ground rules

Read these before opening a PR. They are not style preferences; each one exists
because breaking it has already cost the fleet something.

- **Flat files are the source of truth.** Every store here is plain JSON or JSONL
  on disk under `~/.skcapstone/`, replicated by Syncthing. Never introduce a
  change that makes an index, a cache, or a database the master.
- **One writer per file, always.** Each agent writes only `agents/<agent>.json`;
  each host appends only to `card_events/<host>.jsonl` and
  `archive/<host>.jsonl`; each writer appends only to
  `cards/<id>/events/<agent>@<host>.jsonl`. A new store must follow the same
  pattern. Do not add a shared file that two writers mutate.
- **Every write goes through `atomic_write_text`.** A plain `path.write_text`
  truncates the target and then streams, so a crash (or a Syncthing read) mid
  write leaves a torn file that silently drops a task, an agent record, or a
  vote. See `src/skcoord/atomic_io.py`.
- **Task and card files are effectively immutable.** Status is derived, never
  stored. If you find yourself wanting to add a `status` field to `Task`, stop:
  that is the conflict the whole design exists to avoid.
- **Mutate the raw dict, not the model.** Card updates load the raw JSON dict
  rather than the pydantic model, so unmodelled keys (for example
  `meta.autopilot`) survive a round trip. Round-tripping through the model
  silently deletes them.
- **Additive and optional, always.** Older fleet nodes read the same files. A new
  field must default sensibly when absent, and readers must tolerate a field they
  have never heard of. There is no migration window across a Syncthing mesh.
- **Keep the dependency one way.** `skcapstone` depends on `skcoord`, never the
  reverse at import time. If you need something from skcapstone, import it lazily
  inside the method that uses it. `tests/test_smoke.py::test_imports_do_not_pull_skcapstone`
  enforces this.
- **This stays a pure library.** No CLI, no daemon, no socket, no new runtime
  dependency without a strong reason. `pydantic` is the only one today. The
  user-facing commands belong in `skcapstone`.
- **No secrets, ever.** Card `meta` replicates across the fleet in cleartext.
  `.github/workflows/secret-scan.yml` runs gitleaks over the full history on every
  push and fails the build on a finding. Do not allowlist a real finding.
- **Honest claims.** Do not add a capability or security claim to any doc without
  a backing artifact: a test name, a `file:line`, or a cited spec. See the
  honest-claims gate in
  [sk-standards](https://github.com/smilinTux/sk-standards).

---

## Branch model

- `main` is always releasable, and a push to it **cuts a release tag
  automatically**. Treat every merge as a release.
- Branch per change: `feat/<slug>`, `fix/<slug>`, `docs/<slug>`,
  `security/<slug>`, `test/<slug>`.
- Never commit directly to `main`. Open a PR.
- **Never push a tag by hand.** `publish.yml` computes and pushes the next patch
  tag itself. A hand-cut tag off a feature branch is rejected by the on-main
  backstop in the `build` job, and a hand-cut tag on main can collide with the
  computed one.
- These repos are shared checkouts. Work in a worktree
  (`~/skworld-worktrees/<purpose>-skcoord`) rather than switching branches in a
  checkout another session or service may be using.

---

## Commit convention

- Conventional-style subject: `feat:`, `fix:`, `docs:`, `test:`, `chore:`,
  `security:`.
- Explain **why** in the body, not just what. The commit history here is the only
  record of why a fold rule or a guard exists.
- End every commit message with the attribution trailer, matching the actual
  author model:

  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  ```

- **No em dashes or en dashes** in commit messages, code comments, docs, or PR
  bodies. Use a comma, a parenthesis, a colon, or a new sentence. Regular hyphens
  are fine.

---

## Test gate (must pass before merge)

```bash
~/.skenv/bin/pip install -e . pytest
~/.skenv/bin/python -m pytest tests/ -q
~/.skenv/bin/python -m ruff check src/ tests/
python -m build && python -m twine check dist/*
```

CI runs exactly this in `.github/workflows/ci.yml`: `lint` (blocking, this repo is
ruff-clean), `test` (matrix on Python 3.10 and 3.12, in a clean venv), and
`build`. None of it is softened with `|| true`.

Additional expectations:

- **TDD where there is logic.** Write or extend a test in `tests/` first for any
  new fold rule, status transition, column mapping, or guard. Every existing suite
  in this repo was written that way.
- **Keep the suite hermetic.** Tests take `tmp_path` for their home. No test may
  touch the real `~/.skcapstone/`, the network, or a live host.
- **Test the fold, not just the write.** Event-sourced state is only correct if
  replaying the log produces the right answer, including out-of-order and
  duplicate events.
- Docs-only changes are exempt from new tests, but must keep every claim accurate
  to the code.

---

## Documentation gate

`.github/workflows/docs-check.yml` runs the shared
[sk-standards](https://github.com/smilinTux/sk-standards) validator.

- A change under `src/**` or to `pyproject.toml` must also add a
  `CHANGELOG.md` entry under `## [Unreleased]`.
- If you change a fact the SOP documents (a directory layout, an env var, a WIP
  limit, a CI command, the version mechanism), update `SOP.md` **and** the
  `docs-evidence` block at the end of it in the same PR. Those checks are executed,
  not decorative: they exist so a confident, wrong doc fails loudly.
- Verify locally before pushing:

  ```bash
  python3 path/to/sk-standards/scripts/docs_check.py --repo . --tier 1 --tier 3
  ```

---

## Review path

1. Open a PR against `main`. State what changed, why, and which tests cover it.
2. Confirm `lint`, `test`, `build`, `secret-scan`, and `docs-check` are green, and
   that any behavioural claim in the PR body is reproducible from a named test.
3. A maintainer reviews for sovereignty (no cloud egress, no inlined secrets),
   conflict-freedom (single writer, atomic write, additive schema), correctness of
   the fold, and honest claims.
4. Merge. The push to `main` cuts the next patch tag and publishes to PyPI, so a
   merge is a release. Do not merge something you would not want installed on
   every fleet node.

---

## Releasing

See [SOP.md section 5](./SOP.md). In short: add a dated `CHANGELOG.md` entry,
pass the gate, merge to `main`, and let `publish.yml` cut the tag and publish.
The version comes from the git tag through setuptools-scm; never write a version
number into a file.

---

## Code of Conduct

Participation is governed by [CODE_OF_CONDUCT.md](./CODE_OF_CONDUCT.md).
