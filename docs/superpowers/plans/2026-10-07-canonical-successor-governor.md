# Canonical Successor Governor Implementation Plan

> **For agentic workers:** Execute inline under the active fleet-recovery authorization. Keep all card mutations on the mediated `skcapstone coord` CLI.

**Goal:** Let an exact canonical clone replace one unclaimed noncanonical governed card without weakening normal duplicate prevention.

**Architecture:** Keep the operation within the existing CardStore creation governor. A live predecessor is ignored only when its folded state has `superseded` and `do-not-claim`, its `superseded_by` link names the proposed canonical ID, it has no owner or claim history, and every substantive immutable field matches the new core. Ordinary duplicates, altered successors, canonical predecessors, and in-progress work remain refused.

**Tech Stack:** Python, Pydantic CardCore, CardStore, pytest.

**Spec:** Chef direction in the active Jarvis fleet-fanout objective: preserve stranded noncanonical ready-card work by creating canonical successors through governed coord operations.

## Global Constraints

- Do not mutate CardStore files directly; use coord commands for live cards.
- Do not treat a successor as completed work or alter its dependencies, labels, criteria, or source binding.
- Keep ordinary live duplicate refusals and review depth limits unchanged.
- Only unclaimed backlog/ready noncanonical predecessors can authorize this path.

---

### Task 1: Specify the exact replacement exception

**Files:**
- Modify: `tests/test_card_creation_governor.py`

- [ ] Add a test that creates a noncanonical unclaimed `[REPAIR]` card, folds `superseded`, `do-not-claim`, and `superseded_by=<canonical-id>` events onto it, then creates a canonical successor with identical title, description, criteria, dependencies, priority, swimlane, labels, and metadata.
- [ ] Add refusal assertions for changed content, a canonical predecessor, a missing superseded label, a wrong successor ID, an owned predecessor, and a predecessor with claim history.
- [ ] Run `pytest -q tests/test_card_creation_governor.py`; verify the new exact-clone case fails before implementation.

### Task 2: Implement the narrow governor rule

**Files:**
- Modify: `src/skcoord/card_store.py`

- [ ] Add a private predicate that recognizes only an exact canonical successor for a noncanonical backlog/ready predecessor with the explicit folded supersession link and labels, no owner, and no claim event.
- [ ] Compare immutable task content, excluding only the predecessor and successor IDs and creation timestamps/creator; keep the parent label, title, description, criteria, dependencies, priority, swimlane, labels, and metadata equal.
- [ ] In `_govern_create`, skip the live-duplicate refusal only for that exact predecessor; leave human override and review ancestry behavior intact.
- [ ] Run `pytest -q tests/test_card_creation_governor.py` and `pytest -q tests/test_cardstore_home_guard.py tests/test_atomic_card_mutations.py`.

### Task 3: Validate and publish

**Files:**
- Create: `changelog.d/canonical-superseded-card.md`

- [ ] Run the focused CardStore tests, Ruff, Black check, and `git diff --check`.
- [ ] Commit the plan, regression tests, implementation, and changelog as one focused change.
- [ ] Push the branch, open one PR to `main`, and enable auto-merge with rebase.
- [ ] After rollout, exercise the existing coord link/label/create sequence on one approved noncanonical card and verify both folded cards before continuing any remaining replacements.
