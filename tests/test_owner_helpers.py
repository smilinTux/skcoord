"""Owner-authorized assistance must preserve custody and replay safely."""

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from skcoord.card_store import CardStore, explicit_creation_request_digest
from skcoord.coordination import Board, Task
from skcoord.owner_helpers import parent_contract_digest

# Any reserved route-shaped label, including invalid focused sizes and unknown
# families, is stripped from inherited parent restrictions.
_ROUTE_LABEL = re.compile(r"sk-[a-z]+(?:-[a-z]+)?")

FOCUSED_ROUTES = tuple(
    f"sk-{family}-{size}"
    for family in ("deepseek", "zai", "glm", "codex")
    for size in ("s", "m", "l")
)


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    board = Board(tmp_path)
    parent = Task(
        id="parent01",
        title="[M] Parent outcome",
        created_by="owner",
        tags=["source-only", "codex-only"],
        acceptance_criteria=["Demonstrate the whole outcome"],
        meta={
            "repository": "https://example.com/team/repo.git",
            "base_ref": "main",
            "base_revision": "a" * 40,
        },
    )
    _, revision = board.create_claimed_task(parent, "owner")
    return board, parent, revision


def packet(parent, revision, request="one", paths=None, route="sk-m"):
    # Underscore-prefixed keys are internal store bookkeeping, never part of
    # the owner-facing contract, so a refolded parent must not leak them.
    public_meta = {key: value for key, value in parent.meta.items() if not key.startswith("_")}
    return Task(
        id=hashlib.sha256(f"{parent.id}:{request}".encode()).hexdigest()[:8],
        title="[S] Reproduce the storage failure",
        description="Isolated source only",
        created_by="owner",
        created_at=parent.created_at,
        tags=[
            *[tag for tag in parent.tags if not _ROUTE_LABEL.fullmatch(tag.strip().lower())],
            "owner-helper",
            f"parent-{parent.id}",
            route,
        ],
        dependencies=list(parent.dependencies),
        acceptance_criteria=["Reproduction passes"],
        meta={
            **public_meta,
            "helper_request_id": request,
            "helper_parent_id": parent.id,
            "helper_parent_claim_revision": revision,
            "helper_parent_contract_sha256": parent_contract_digest(parent),
            "helper_objective": "Reproduce the failure",
            "helper_allowed_paths": ["tests/storage.py"] if paths is None else paths,
            "helper_verification": ["pytest tests/storage.py"],
            "logical_route": route,
        },
    )


def create(board, parent, revision, helper, actor="owner"):
    digest = explicit_creation_request_digest(helper.model_dump(mode="json"))
    return board.create_helper_task(helper, digest, actor, parent.id, revision)


def test_active_owner_delegates_without_changing_parent(scenario):
    board, parent, revision = scenario
    store = CardStore(board.home)
    before = store.fold(parent.id).model_dump()
    helper = packet(parent, revision)
    path = create(board, parent, revision, helper)
    assert path.exists()
    assert store.fold(parent.id).model_dump() == before
    child = store.fold(helper.id)
    assert child.owner is None and child.status.value == "backlog"
    assert child.meta["base_revision"] == "a" * 40
    assert child.acceptance_criteria == helper.acceptance_criteria
    assert f"parent-{parent.id}" in child.labels


def test_exact_replay_has_one_child_and_no_new_events(scenario):
    board, parent, revision = scenario
    helper = packet(parent, revision)
    first = create(board, parent, revision, helper)
    before = {str(p): p.read_bytes() for p in (board.home / "cards").rglob("*.jsonl")}
    assert create(board, parent, revision, helper) == first
    after = {str(p): p.read_bytes() for p in (board.home / "cards").rglob("*.jsonl")}
    assert after == before


def test_changed_request_cannot_reuse_id(scenario):
    board, parent, revision = scenario
    helper = packet(parent, revision)
    create(board, parent, revision, helper)
    helper.acceptance_criteria = ["Different outcome"]
    with pytest.raises(ValueError, match="conflict"):
        create(board, parent, revision, helper)


@pytest.mark.parametrize("change", ["stale", "foreign", "conflict", "done", "void"])
def test_invalid_parent_has_no_child(scenario, change):
    board, parent, revision = scenario
    store = CardStore(board.home)
    helper = packet(parent, revision)
    actor = "owner"
    if change == "stale":
        revision = "stale-revision"
    elif change == "foreign":
        actor = "other"
    elif change == "conflict":
        store.append_event(parent.id, "claim", "other", owner="other", claim_revision="other")
    elif change == "done":
        store.append_event(parent.id, "move", "owner", column="done")
    else:
        store.append_event(parent.id, "void", "owner", reason="cancelled")
    with pytest.raises(ValueError):
        create(board, parent, revision, helper, actor)
    assert store.fold(helper.id) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("base_ref", 123),
        ("base_revision", "b" * 40),
        ("repository", "https://different.example/repo.git"),
        ("helper_parent_claim_revision", "old"),
        ("helper_allowed_paths", ["../outside"]),
        ("helper_allowed_paths", ["src/*"]),
        ("helper_verification", []),
    ],
)
def test_malformed_or_expanded_packet_leaves_no_child(scenario, field, value):
    board, parent, revision = scenario
    helper = packet(parent, revision)
    helper.meta[field] = value
    with pytest.raises(ValueError):
        create(board, parent, revision, helper)
    assert CardStore(board.home).fold(helper.id) is None


@pytest.mark.parametrize("route", ["sk-s", "sk-m", "sk-l", "sk-xl"])
def test_supported_routes_are_accepted(scenario, route):
    board, parent, revision = scenario
    helper = packet(parent, revision, request=route, route=route)
    create(board, parent, revision, helper)
    child = CardStore(board.home).fold(helper.id)
    assert child is not None
    assert child.meta["logical_route"] == route
    assert route in child.labels


@pytest.mark.parametrize(
    "route",
    [
        pytest.param(None, id="missing-null"),
        pytest.param(7, id="non-string"),
        pytest.param("SK-S", id="case-variant"),
        pytest.param(" sk-s", id="padded-left"),
        pytest.param("sk-s ", id="padded-right"),
        pytest.param("sk-m\n", id="newline"),
        pytest.param("", id="empty"),
        pytest.param("sk-xs", id="unknown"),
        pytest.param("sk-m|sk-xl", id="multi-route"),
        pytest.param("sk-m sk-xl", id="space-separated"),
    ],
)
def test_bad_logical_route_leaves_no_child(scenario, route):
    board, parent, revision = scenario
    helper = packet(parent, revision, route="sk-m")
    # Drop the default route tag so the route failure is isolated.
    helper.tags = [tag for tag in helper.tags if tag != "sk-m"]
    if route is None:
        helper.meta.pop("logical_route")
    else:
        helper.meta["logical_route"] = route
    # A missing field fails the metadata-contract check; a bad value fails
    # the route check. Both must leave no child behind.
    expected_match = "contract fields" if route is None else "logical_route"
    with pytest.raises(ValueError, match=expected_match):
        create(board, parent, revision, helper)
    assert CardStore(board.home).fold(helper.id) is None


def test_inherited_route_label_is_removed_other_labels_kept(scenario):
    board, parent, revision = scenario
    store = CardStore(board.home)
    # Legacy fixture: the parent already carries the exact supported route
    # labels sk-l and sk-xl plus an unrelated prefixed label and a custom
    # restriction. The helper must strip only the exact old route labels and
    # keep everything else, replacing them with the metadata-selected route.
    for label in (
        "sk-l",
        "sk-xl",
        "parent-sk-l",
        "inherited-sk-xl",
        "custom-restriction",
    ):
        store.append_event(parent.id, "add_label", "owner", label=label)
    # The scenario's parent object and revision were computed before these
    # events, so refold the parent and recompute the contract digest; the
    # helper must bind the current claim revision, not a stale one.
    card = store.fold(parent.id)
    parent = Task.model_validate(
        {
            "id": card.id,
            "title": card.title,
            "description": card.description,
            "created_by": card.originator,
            "created_at": card.created_at,
            "tags": card.labels,
            "dependencies": card.dependencies,
            "acceptance_criteria": card.acceptance_criteria,
            "meta": card.meta,
        }
    )
    revision = card.meta["_claim_revision"]
    helper = packet(parent, revision, route="sk-s")
    create(board, parent, revision, helper)
    child = store.fold(helper.id)
    assert child.meta["logical_route"] == "sk-s"
    assert set(child.labels) == {
        "source-only",
        "codex-only",
        "parent-sk-l",
        "inherited-sk-xl",
        "custom-restriction",
        "owner-helper",
        f"parent-{parent.id}",
        "sk-s",
    }


@pytest.mark.parametrize(
    "tags,match",
    [
        pytest.param(
            ["sk-m", "sk-m"],
            "occurrence",
            id="duplicate-selected-route",
        ),
        pytest.param(
            ["sk-s", "sk-m"],
            "second exact route",
            id="duplicate-other-route",
        ),
    ],
)
def test_duplicate_route_tags_leave_no_child(scenario, tags, match):
    board, parent, revision = scenario
    helper = packet(parent, revision, route="sk-m")
    helper.tags = [*parent.tags, "owner-helper", f"parent-{parent.id}", *tags]
    with pytest.raises(ValueError, match=match):
        create(board, parent, revision, helper)
    assert CardStore(board.home).fold(helper.id) is None


@pytest.mark.parametrize("label", [" sk-m ", "SK-L", "sk-xl\t"])
def test_padded_case_tab_inherited_routes_removed(scenario, label):
    board, parent, revision = scenario
    store = CardStore(board.home)
    for extra in ("parent-sk-l", "inherited-sk-xl"):
        store.append_event(parent.id, "add_label", "owner", label=extra)
    if label not in parent.tags:
        store.append_event(parent.id, "add_label", "owner", label=label)
    card = store.fold(parent.id)
    parent = Task.model_validate(
        {
            "id": card.id,
            "title": card.title,
            "description": card.description,
            "created_by": card.originator,
            "created_at": card.created_at,
            "tags": card.labels,
            "dependencies": card.dependencies,
            "acceptance_criteria": card.acceptance_criteria,
            "meta": card.meta,
        }
    )
    revision = card.meta["_claim_revision"]
    helper = packet(parent, revision, route="sk-s")
    create(board, parent, revision, helper)
    child = store.fold(helper.id)
    assert child is not None
    assert child.meta["logical_route"] == "sk-s"
    # The padded/case/tab variant is removed as an inherited route label,
    # while the prefixed lookalikes and all other restrictions survive
    # byte-exact, and the selected route tag is present exactly once.
    assert label not in child.labels
    assert "parent-sk-l" in child.labels
    assert "inherited-sk-xl" in child.labels
    assert child.labels.count("sk-s") == 1
    assert "owner-helper" in child.labels
    assert f"parent-{parent.id}" in child.labels
    assert set(child.labels) == {
        "source-only",
        "codex-only",
        "parent-sk-l",
        "inherited-sk-xl",
        "owner-helper",
        f"parent-{parent.id}",
        "sk-s",
    }


@pytest.mark.parametrize("tags", [["sk-xl"], ["sk-s", "sk-xl"]])
def test_route_label_mismatch_refused(scenario, tags):
    board, parent, revision = scenario
    helper = packet(parent, revision, route="sk-s")
    helper.tags = [*parent.tags, "owner-helper", f"parent-{parent.id}", *tags]
    with pytest.raises(ValueError):
        create(board, parent, revision, helper)
    assert CardStore(board.home).fold(helper.id) is None


def test_restrictions_cannot_be_dropped_or_granted(scenario):
    board, parent, revision = scenario
    for labels in [
        ["source-only"],
        [*parent.tags, "dispatch-approved", f"parent-{parent.id}"],
    ]:
        helper = packet(parent, revision)
        helper.tags = labels
        with pytest.raises(ValueError):
            create(board, parent, revision, helper)


def test_child_cannot_depend_on_parent(scenario):
    board, parent, revision = scenario
    helper = packet(parent, revision)
    helper.dependencies = [parent.id]
    with pytest.raises(ValueError):
        create(board, parent, revision, helper)


def test_scope_overlap_refused_but_read_only_helper_allowed(scenario):
    board, parent, revision = scenario
    create(board, parent, revision, packet(parent, revision, paths=["tests"]))
    with pytest.raises(ValueError, match="overlap"):
        create(board, parent, revision, packet(parent, revision, "two"))
    create(board, parent, revision, packet(parent, revision, "read-only", paths=[]))


def test_generation_rechecked_after_board_lock_acquired(scenario, monkeypatch):
    import skcoord.coordination as module

    board, parent, revision = scenario
    original = module._board_mutation_lock

    @contextmanager
    def changed_before_lock(home):
        CardStore(home).append_event(
            parent.id, "claim", "owner", owner="owner", claim_revision="new-generation"
        )
        with original(home):
            yield

    monkeypatch.setattr(module, "_board_mutation_lock", changed_before_lock)
    helper = packet(parent, revision)
    with pytest.raises(ValueError):
        create(board, parent, revision, helper)
    assert CardStore(board.home).fold(helper.id) is None


def test_projection_write_interruption_replays_existing_explicit_creation(scenario, monkeypatch):
    import skcoord.coordination as module

    board, parent, revision = scenario
    helper = packet(parent, revision)
    original = module.atomic_write_text

    def fail(path, data):
        if path.parent == board.tasks_dir:
            raise OSError("disk full")
        return original(path, data)

    with monkeypatch.context() as patch:
        patch.setattr(module, "atomic_write_text", fail)
        with pytest.raises(OSError, match="disk full"):
            create(board, parent, revision, helper)
    assert CardStore(board.home).fold(helper.id) is not None
    assert create(board, parent, revision, helper).exists()


def test_simultaneous_overlapping_helpers_admit_only_one(scenario):
    board, parent, revision = scenario

    def submit(request):
        try:
            return str(create(board, parent, revision, packet(parent, revision, request)))
        except ValueError as exc:
            assert "overlap" in str(exc)
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, ["parallel-one", "parallel-two"]))
    assert sum(result is not None for result in results) == 1


def test_external_dependencies_cannot_be_removed(scenario):
    board, parent, revision = scenario
    store = CardStore(board.home)
    board.create_task(Task(id="wait001", title="[S] Upstream prerequisite"))
    store.append_event(parent.id, "add_dependency", "owner", dependency="wait001")
    helper = packet(parent, revision)
    with pytest.raises(ValueError, match="external dependencies"):
        create(board, parent, revision, helper)
    helper.dependencies = ["wait001"]
    create(board, parent, revision, helper)
    with pytest.raises(ValueError, match="[Dd]ependenc|blocked|Blocked"):
        board.claim_task("helper-agent", helper.id)


def test_conflicting_source_projection_is_not_silently_chosen(scenario):
    board, parent, revision = scenario
    CardStore(board.home).append_event(
        parent.id, "link", "owner", link_key="base_revision", link_value="b" * 40
    )
    with pytest.raises(ValueError, match="conflicting"):
        create(board, parent, revision, packet(parent, revision))


def test_helper_completion_does_not_complete_parent(scenario):
    board, parent, revision = scenario
    store = CardStore(board.home)
    helper = packet(parent, revision)
    create(board, parent, revision, helper)
    before = store.fold(parent.id).model_dump()
    store.append_event(helper.id, "move", "helper-agent", column="done")
    assert store.fold(parent.id).model_dump() == before


def test_forged_digest_is_refused_before_creation(scenario):
    board, parent, revision = scenario
    helper = packet(parent, revision)
    with pytest.raises(ValueError, match="digest"):
        board.create_helper_task(helper, "f" * 64, "owner", parent.id, revision)
    assert CardStore(board.home).fold(helper.id) is None


@pytest.mark.parametrize("label", ["review", "REVIEW", "Seat-Seraph"])
def test_public_api_rejects_review_custody_labels(scenario, label):
    board, parent, revision = scenario
    CardStore(board.home).append_event(parent.id, "add_label", "owner", label=label)
    with pytest.raises(ValueError, match="governed reviewers"):
        create(board, parent, revision, packet(parent, revision))


def test_public_api_rejects_mixed_size_markers(scenario):
    board, parent, revision = scenario
    helper = packet(parent, revision)
    helper.title = "[S][XL] Ambiguous route"
    with pytest.raises(ValueError, match="bounded source title"):
        create(board, parent, revision, helper)


def test_parent_contract_amendment_invalidates_prepared_request(scenario):
    board, parent, revision = scenario
    helper = packet(parent, revision)
    CardStore(board.home).append_event(
        parent.id, "describe", "owner", description="A newly discovered restriction"
    )
    with pytest.raises(ValueError, match="contract changed"):
        create(board, parent, revision, helper)
    assert CardStore(board.home).fold(helper.id) is None


@pytest.mark.parametrize("route", FOCUSED_ROUTES)
def test_focused_routes_are_accepted(scenario, route):
    board, parent, revision = scenario
    helper = packet(parent, revision, request=route, route=route)
    create(board, parent, revision, helper)
    child = CardStore(board.home).fold(helper.id)
    assert child is not None
    assert child.meta["logical_route"] == route
    assert child.labels.count(route) == 1
    assert set(child.labels) == {
        "source-only",
        "codex-only",
        "owner-helper",
        f"parent-{parent.id}",
        route,
    }


@pytest.mark.parametrize(
    "route",
    [
        pytest.param("sk-deepseek-xl", id="focused-xl-deepseek"),
        pytest.param("sk-zai-xl", id="focused-xl-zai"),
        pytest.param("sk-glm-xl", id="focused-xl-glm"),
        pytest.param("sk-codex-xl", id="focused-xl-codex"),
        pytest.param("sk-llama-s", id="unknown-family"),
        pytest.param("sk-mistral-m", id="unknown-family-m"),
        pytest.param("sk-ds-s", id="family-alias"),
        pytest.param("sk-deepseek", id="missing-size"),
        pytest.param("sk-deepseek-ss", id="unknown-size"),
        pytest.param("deepseek-s", id="missing-prefix"),
        pytest.param("SK-DEEPSEEK-S", id="upper-case"),
        pytest.param("Sk-Deepseek-S", id="mixed-case"),
        pytest.param(" sk-deepseek-s", id="padded-left"),
        pytest.param("sk-deepseek-s ", id="padded-right"),
        pytest.param("sk-deepseek-s\n", id="newline"),
        pytest.param("sk-deepseek\ts", id="embedded-tab"),
        pytest.param(7, id="non-string-int"),
        pytest.param(True, id="non-string-bool"),
    ],
)
def test_bad_focused_route_leaves_no_child(scenario, route):
    board, parent, revision = scenario
    helper = packet(parent, revision, route="sk-m")
    # Drop the default route tag so the route failure is isolated.
    helper.tags = [tag for tag in helper.tags if tag != "sk-m"]
    helper.meta["logical_route"] = route
    with pytest.raises(ValueError, match="logical_route"):
        create(board, parent, revision, helper)
    assert CardStore(board.home).fold(helper.id) is None


@pytest.mark.parametrize(
    "route,tags,match",
    [
        pytest.param(
            "sk-deepseek-s",
            ["sk-deepseek-s", "sk-deepseek-s"],
            "occurrence",
            id="duplicate-selected-focused-route",
        ),
        pytest.param(
            "sk-deepseek-s",
            ["sk-codex-s"],
            "occurrence",
            id="focused-metadata-label-mismatch",
        ),
        pytest.param(
            "sk-deepseek-s",
            ["sk-s"],
            "occurrence",
            id="generic-label-for-focused-metadata",
        ),
        pytest.param(
            "sk-m",
            ["sk-deepseek-s"],
            "occurrence",
            id="focused-label-for-generic-metadata",
        ),
        pytest.param(
            "sk-deepseek-s",
            ["sk-deepseek-s", "sk-zai-m"],
            "second exact route",
            id="two-distinct-focused-routes",
        ),
        pytest.param(
            "sk-m",
            ["sk-m", "sk-glm-l"],
            "second exact route",
            id="generic-plus-focused-route",
        ),
        pytest.param(
            "sk-deepseek-s",
            ["sk-deepseek-xl"],
            "occurrence",
            id="focused-xl-label-not-selected",
        ),
        pytest.param(
            "sk-deepseek-s",
            ["sk-llama-s"],
            "occurrence",
            id="unknown-family-label-not-selected",
        ),
    ],
)
def test_focused_route_ambiguity_refused(scenario, route, tags, match):
    board, parent, revision = scenario
    helper = packet(parent, revision, route=route)
    helper.tags = [*parent.tags, "owner-helper", f"parent-{parent.id}", *tags]
    with pytest.raises(ValueError, match=match):
        create(board, parent, revision, helper)
    assert CardStore(board.home).fold(helper.id) is None


def test_inherited_focused_route_labels_removed(scenario):
    board, parent, revision = scenario
    store = CardStore(board.home)
    # The parent may already carry focused route labels from its own generation.
    # Only reserved route shapes are replaced; prefixed lookalikes survive.
    for label in (
        "sk-deepseek-s",
        "sk-codex-l",
        "sk-deepseek-xl",
        "parent-sk-deepseek-s",
        "inherited-sk-codex-l",
        "custom-restriction",
    ):
        store.append_event(parent.id, "add_label", "owner", label=label)
    card = store.fold(parent.id)
    parent = Task.model_validate(
        {
            "id": card.id,
            "title": card.title,
            "description": card.description,
            "created_by": card.originator,
            "created_at": card.created_at,
            "tags": card.labels,
            "dependencies": card.dependencies,
            "acceptance_criteria": card.acceptance_criteria,
            "meta": card.meta,
        }
    )
    revision = card.meta["_claim_revision"]
    helper = packet(parent, revision, route="sk-glm-m")
    create(board, parent, revision, helper)
    child = store.fold(helper.id)
    assert child is not None
    assert child.meta["logical_route"] == "sk-glm-m"
    assert child.labels.count("sk-glm-m") == 1
    for stripped in ("sk-deepseek-s", "sk-codex-l", "sk-deepseek-xl"):
        assert stripped not in child.labels
    assert "parent-sk-deepseek-s" in child.labels
    assert "inherited-sk-codex-l" in child.labels
    assert set(child.labels) == {
        "source-only",
        "codex-only",
        "custom-restriction",
        "parent-sk-deepseek-s",
        "inherited-sk-codex-l",
        "owner-helper",
        f"parent-{parent.id}",
        "sk-glm-m",
    }
