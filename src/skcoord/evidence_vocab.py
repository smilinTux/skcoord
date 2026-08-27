#!/usr/bin/env python3
"""Controlled vocabulary for CardStore evidence link_key values.

WHY THIS EXISTS. The evidence store carries 19404 link events using 3674 distinct
link_key values. The top 100 keys cover only 49 percent. 849 keys are used exactly
once. 125 embed a timestamp in the key name. 223 concepts are spelled two or more
ways, differing only by hyphen, underscore or case.

That is free text in a key position, and it has already caused a real failure: a
card was ranked highest-leverage by downstream dependency count while it was in
fact superseded twice, with an expired credential revision and a canary that had
returned BLOCKED_FAIL_CLOSED. The supersession WAS recorded, in one of 22 spellings
the reader did not check. Verdict has 43 spellings; a reader using only the
canonical key sees 356 of roughly 600 verdict-bearing events.

THE DESIGN, three layers:
  1. CORE: a small typed vocabulary readers may rely on.
  2. ALIASES: every observed spelling of a core concept folds to one canonical key,
     so a reader asking for "superseded_by" gets all 22 variants.
  3. ESCAPE HATCH: genuinely card-specific facts use x-<agent>-<key>. The 3300-key
     tail belongs here, and is explicitly NOT a schema violation.

Normalization is deliberately lossy in one direction only: it never invents a
concept, it only folds spellings. Anything it cannot confidently fold stays
uncontrolled and is REPORTED rather than silently reinterpreted, because
misreading an unknown key as a known one is exactly the failure above.
"""
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Iterable

# --- layer 1: typed core -----------------------------------------------------
# value_type: sha256 | commit | path | text | enum | card_id | url | bool
CORE = {
    # outcome. Never infer these from lifecycle state or from links alone.
    "verdict":            {"value_type": "enum", "enum": ["PASS", "PASS_FOR_REVIEW", "BLOCKED", "FAIL"]},
    "disposition":        {"value_type": "text"},
    "review_decision":    {"value_type": "text"},
    "result":             {"value_type": "text"},
    "blocker":            {"value_type": "text"},
    "blocked_by":         {"value_type": "card_id"},
    "blocked_on":         {"value_type": "text"},
    "limitations":        {"value_type": "text"},

    # provenance of the artifact the outcome is about
    "evidence":           {"value_type": "path"},
    "evidence_sha256":    {"value_type": "sha256"},
    "evidence_commit":    {"value_type": "commit"},
    "evidence_tree":      {"value_type": "commit"},
    "manifest_sha256":    {"value_type": "sha256"},
    "candidate":          {"value_type": "path"},
    "candidate_commit":   {"value_type": "commit"},
    "candidate_tree":     {"value_type": "commit"},
    "commit":             {"value_type": "commit"},
    "tree":               {"value_type": "commit"},
    "merge_commit":       {"value_type": "commit"},
    "pr":                 {"value_type": "url"},
    "files":              {"value_type": "text"},
    "hashes":             {"value_type": "text"},

    # lifecycle relationships between cards. THE class that caused the failure.
    "superseded_by":      {"value_type": "card_id"},
    "replaces":           {"value_type": "card_id"},
    "predecessor":        {"value_type": "card_id"},
    "successor":          {"value_type": "card_id"},
    "replacement":        {"value_type": "card_id"},
    "followup_card":      {"value_type": "card_id"},

    # qualification
    "tests":              {"value_type": "text"},
    "tdd":                {"value_type": "path"},
    "verification":       {"value_type": "text"},
    "acceptance":         {"value_type": "text"},
    "independent_review": {"value_type": "text"},
    "ci":                 {"value_type": "text"},

    # operational safety
    "rollback":           {"value_type": "text"},
    "safe_state":         {"value_type": "text"},
    "scope":              {"value_type": "text"},
    "execution_boundary": {"value_type": "text"},
    "prohibited_actions": {"value_type": "text"},

    # APPROVAL. Safety-critical and deliberately separate from verdict: a verdict is
    # an agent's finding, an approval is a human authorising action on it. No agent
    # may write human_approval. A reader must find every spelling of it.
    "human_approval":     {"value_type": "text"},

    # review, distinct from the agent's own verdict
    "review":             {"value_type": "text"},
    "review_commit":      {"value_type": "commit"},
    "review_tree":        {"value_type": "commit"},
    "review_evidence":    {"value_type": "path"},
    "qualification":      {"value_type": "text"},
    "gate_status":        {"value_type": "text"},

    # source provenance, distinct from candidate
    "source_commit":      {"value_type": "commit"},
    "source_tree":        {"value_type": "commit"},
    "manifest":           {"value_type": "path"},
    "release":            {"value_type": "text"},
    "publish":            {"value_type": "text"},
    "doc":                {"value_type": "path"},
    "tag":                {"value_type": "text"},
    "repair":             {"value_type": "text"},

    # execution context: WHERE the work ran. Load-bearing for reproducibility.
    "worktree":           {"value_type": "path"},
    "cwd":                {"value_type": "path"},
    "pane":               {"value_type": "text"},
    "runtime":            {"value_type": "text"},
    "route":              {"value_type": "text"},
    "startup_log":        {"value_type": "path"},
    "merge":              {"value_type": "text"},
    "architecture_evidence": {"value_type": "path"},
}

# --- layer 2: alias folding ---------------------------------------------------
# Ordered: first match wins. Anchored on concept stems seen in live data.
_ALIAS_RULES = [
    (r"^(superseded|superseding|supersedes)(_|$)|^replaces$", "superseded_by"),
    (r"(^|_)(review_)?verdict(_|$)",                    "verdict"),
    (r"^result$|^review_decision$",                     "verdict"),
    (r"^review_disposition(_|$)",                       "review_decision"),
    (r"^disposition(_|$)",                              "disposition"),
    (r"^evidence_sha256$|^evidence_hash$",              "evidence_sha256"),
    (r"^evidence(_doc|_path|_file)?$",                  "evidence"),
    (r"^evidence_(commit|sha)$",                        "evidence_commit"),
    (r"^candidate_commit$",                             "candidate_commit"),
    (r"^candidate_tree$",                               "candidate_tree"),
    (r"^candidate(_|$)",                                "candidate"),
    (r"^merge_commit$",                                 "merge_commit"),
    (r"^(exact_)?base_?commit$|^commit$",               "commit"),
    (r"^tree$|^base_tree$",                             "tree"),
    (r"^manifest_sha256$|^manifest_hash$",              "manifest_sha256"),
    (r"^blocked_by(_|$)",                               "blocked_by"),
    (r"^blocker(s)?(_|$)",                              "blocker"),
    (r"^predecessor(_|$)",                              "predecessor"),
    (r"^successor(_|$)",                                "successor"),
    (r"^replacement(_|$)|^replaces(_|$)",               "replaces"),
    (r"^followup_card$|^follow_up_card$",               "followup_card"),
    (r"^tests?(_|$)",                                   "tests"),
    (r"^tdd(_|$)",                                      "tdd"),
    (r"^independent_(re)?review(_|$)",                  "independent_review"),
    (r"^rollback(_|$)",                                 "rollback"),
    (r"^limitations?(_|$)",                             "limitations"),
    (r"^safe_state(_|$)",                               "safe_state"),
    (r"^acceptance(_|$)",                               "acceptance"),
    (r"^verification(_|$)",                             "verification"),
    (r"^scope(_|$)",                                    "scope"),
    (r"^execution_boundary(_|$)",                       "execution_boundary"),
    (r"^result(_|$)",                                   "result"),
    (r"^pr(_|$)",                                       "pr"),
    (r"^ci(_|$)",                                       "ci"),
    # approval: safety-critical, folded before the generic review rules
    (r"^human_approval(_|$)|^approval(_|$)|^approved_by(_|$)", "human_approval"),
    (r"^review_commit$",                                "review_commit"),
    (r"^review_tree$",                                  "review_tree"),
    (r"^review_(evidence|bundle)(_|$)",                 "review_evidence"),
    (r"^review(_|$)",                                   "review"),
    (r"^qualification(_|$)",                            "qualification"),
    (r"^gate_status(_|$)|^gate(_|$)",                   "gate_status"),
    (r"^source_commit$",                                "source_commit"),
    (r"^source_tree$",                                  "source_tree"),
    (r"^manifest(_|$)",                                 "manifest"),
    (r"^release(_|$)",                                  "release"),
    (r"^publish(_|$)|^publication(_|$)",                "publish"),
    (r"^doc(s|umentation)?(_|$)",                       "doc"),
    (r"^tag(s)?(_|$)",                                  "tag"),
    (r"^repair(_|$)",                                   "repair"),
    (r"^worktree(_|$)",                                 "worktree"),
    (r"^cwd(_|$)",                                      "cwd"),
    (r"^pane(_|$)",                                     "pane"),
    (r"^runtime(_|$)",                                  "runtime"),
    (r"^route(_|$)",                                    "route"),
    (r"^startup_log(_|$)",                              "startup_log"),
    (r"^merge(_|$)",                                    "merge"),
    (r"^architecture_evidence(_|$)",                    "architecture_evidence"),
    (r"^prohibited_actions?(_|$)",                      "prohibited_actions"),
    (r"(^|_)blocker(s)?(_|$)",                          "blocker"),
    (r"^focused_tests?(_|$)",                           "tests"),
]
_COMPILED = [(re.compile(p), c) for p, c in _ALIAS_RULES]

ESCAPE = re.compile(r"^x-[a-z0-9][a-z0-9_.-]*-[a-z0-9][a-z0-9_.-]*$")


def normalize(key: str) -> str:
    """Fold spelling only. Case, separator, embedded timestamp/hash/version."""
    k = (key or "").strip().lower().replace("-", "_")
    k = re.sub(r"_?20\d{6}t?\d{0,6}z?", "", k)   # 20260826T021646Z
    k = re.sub(r"_[0-9a-f]{8,64}$", "", k)        # trailing hash
    k = re.sub(r"_v\d+(_\d+)*", "", k)            # _v1, _v1_1_2
    k = re.sub(r"__+", "_", k).strip("_")
    return k


def canonical(key: str):
    """Return (canonical_key, status).

    status is one of:
      core     already a core key
      aliased  folded to a core key by a rule
      escape   valid namespaced x- key, intentionally uncontrolled
      unknown  NOT interpreted. Reported, never guessed at.
    """
    raw = (key or "").strip()
    if ESCAPE.match(raw):
        return raw, "escape"
    n = normalize(raw)
    # Safety aliases run before the core lookup. Historical ``result``,
    # ``review-decision`` and forward ``supersedes`` links are members of one
    # verdict or supersession concept, not competing canonical concepts.
    for rx, target in _COMPILED:
        if rx.search(n):
            if n == target and raw == target:
                return target, "core"
            return target, "aliased"
    if n in CORE:
        return n, "core" if raw == n else "aliased"
    return n, "unknown"


def canonical_key(key: str) -> str:
    """Return the normalized canonical key used by all evidence readers."""
    return canonical(key)[0]


def read_links(paths: Iterable[Path], card_id: str | None = None) -> dict[str, list[dict]]:
    """Read link events grouped by canonical key without losing aliases.

    Every nonblank line is parsed as JSON. Malformed lines fail closed instead
    of disappearing from safety checks. Structural CardStore state is not read
    here and no verdict is inferred from lifecycle state or link presence.
    """
    grouped: dict[str, list[dict]] = defaultdict(list)
    rows: list[dict] = []
    for path in sorted(Path(value) for value in paths):
        with path.open(encoding="utf-8") as source:
            for number, line in enumerate(source, 1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{number} is malformed JSON") from exc
                if not isinstance(event, dict):
                    raise ValueError(f"{path}:{number} must contain a JSON object")
                if event.get("action") != "link":
                    continue
                if card_id is not None and event.get("card_id") != card_id:
                    continue
                rows.append(event)
    rows.sort(key=lambda event: (
        str(event.get("ts") or ""),
        str(event.get("writer") or ""),
        str(event.get("event_id") or ""),
    ))
    for event in rows:
        key = canonical_key(str(event.get("link_key") or ""))
        grouped[key].append(event)
    return dict(grouped)


def is_valid_for_write(key: str) -> tuple:
    """Write-time gate. Returns (ok, reason)."""
    raw = (key or "").strip()
    if not raw:
        return False, "empty link_key"
    if re.search(r"20\d{6}", raw):
        return False, "timestamp in key name; put it in the value"
    if re.search(r"[0-9a-f]{16,}", raw):
        return False, "hash in key name; put it in the value"
    if raw != raw.lower():
        return False, "key must be lowercase"
    if "-" in raw and not ESCAPE.match(raw):
        return False, "use underscore, or the x-<agent>-<key> escape form"
    c, status = canonical(raw)
    if status == "unknown":
        return False, "uncontrolled key; use a core key or x-<agent>-%s" % normalize(raw)
    return True, status


def validate_for_write(key: str) -> str:
    """Validate a new key and return its canonical representation.

    Existing historical aliases remain readable, but new writes must already use
    a canonical core key or a namespaced ``x-<agent>-<key>`` escape key.
    """
    ok, reason = is_valid_for_write(key)
    canonical_value, status = canonical(key)
    if not ok:
        raise ValueError(f"invalid link_key {key!r}: {reason}")
    if status == "aliased":
        raise ValueError(
            f"invalid link_key {key!r}: use canonical key {canonical_value!r}"
        )
    return canonical_value
