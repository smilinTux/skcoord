"""Revision-bound, read-only reconciliation plan for dependencies on void cards."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from .lifecycle_reassessment import (
    CardRecord,
    _dependencies,
    _superseded_ids,
    load_cards,
    load_evidence,
)

_SCHEMA = "skcoord.void-edge-reconciliation/v1"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _revision(record: CardRecord) -> str:
    """Hash all birth facts and ordered events used to produce a folded card."""
    return hashlib.sha256(
        _canonical({"core": record.core, "events": list(record.events)})
    ).hexdigest()


def _report_hash(report: dict[str, Any]) -> str:
    declared = report.get("content_sha256")
    body = dict(report)
    body.pop("content_sha256", None)
    actual = hashlib.sha256(_canonical(body)).hexdigest()
    if declared != actual:
        raise ValueError("assessment content_sha256 does not match its canonical content")
    return actual


def build_manifest(cards_dir: Path, evidence_dir: Path, assessment_path: Path) -> dict[str, Any]:
    """Fresh-fold and classify every assessed edge without writing CardStore."""
    assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
    assessment_hash = _report_hash(assessment)
    assessed = assessment.get("classes", {}).get("void_dependency_edges")
    if not isinstance(assessed, list):
        raise ValueError("assessment has no void_dependency_edges list")

    records = load_cards(cards_dir)
    evidence = load_evidence(evidence_dir)
    successors = _superseded_ids(records, evidence)
    pair_counts: dict[tuple[str, str], int] = {}
    for item in assessed:
        pair = (str(item["card_id"]), str(item["void_dependency_id"]))
        pair_counts[pair] = pair_counts.get(pair, 0) + 1

    actions = []
    for ordinal, item in enumerate(assessed, 1):
        dependent_id = str(item["card_id"])
        void_id = str(item["void_dependency_id"])
        dependent = records.get(dependent_id)
        void = records.get(void_id)
        if dependent is None or void is None:
            raise ValueError(f"assessed edge references missing card: {dependent_id}->{void_id}")
        if void_id not in _dependencies(dependent) or "void" not in {
            str(event.get("action")) for event in void.events
        }:
            raise ValueError(f"assessed edge is no longer a dependency on a void card: {dependent_id}->{void_id}")

        candidate_successors = successors.get(void_id, [])
        if pair_counts[(dependent_id, void_id)] > 1:
            classification = "duplicate historical edge"
            replacement = None
        elif len(candidate_successors) == 1 and candidate_successors[0] != dependent_id:
            classification = "successor substitution"
            replacement = candidate_successors[0]
        elif candidate_successors:
            classification = "human decision required"
            replacement = None
        else:
            classification = "remove"
            replacement = None

        actions.append(
            {
                "ordinal": ordinal,
                "dependent_id": dependent_id,
                "dependent_revision": _revision(dependent),
                "void_id": void_id,
                "void_revision": _revision(void),
                "classification": classification,
                "successor_id": replacement,
                "successor_candidates": candidate_successors,
            }
        )

    # Reject a store that drifted while it was being folded and classified.
    fresh = load_cards(cards_dir)
    bound_ids = {value for row in actions for value in (row["dependent_id"], row["void_id"])}
    for card_id in bound_ids:
        if card_id not in fresh or _revision(fresh[card_id]) != _revision(records[card_id]):
            raise RuntimeError(f"CardStore drift detected for {card_id}")

    counts: dict[str, int] = {}
    for action in actions:
        key = action["classification"]
        counts[key] = counts.get(key, 0) + 1
    manifest = {
        "schema": _SCHEMA,
        "read_only": True,
        "assessment_content_sha256": assessment_hash,
        "assessment_generated_at": assessment.get("generated_at"),
        "edge_count": len(actions),
        "classification_counts": dict(sorted(counts.items())),
        "actions": actions,
    }
    manifest["content_sha256"] = hashlib.sha256(_canonical(manifest)).hexdigest()
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cards-dir", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--assessment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = build_manifest(args.cards_dir, args.evidence_dir, args.assessment)
    encoded = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    # Parse serializer output before publishing it. This is not a CardStore append.
    json.loads(encoded)
    args.output.write_text(encoded, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
