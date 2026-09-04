"""Versioned tier-one validation for immutable CardStore core records.

The validator is deliberately isolated from CardStore reads and lifecycle folds:
it validates only ``core.json`` birth facts.  Structural state and evidence
verdicts remain separate event streams and are never inferred here.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterable, Mapping

from jsonschema import Draft202012Validator, FormatChecker

_SCHEMA_RESOURCE = "schemas/card-core.v1.schema.json"


@dataclass(frozen=True)
class CoreValidationFailure:
    """One attributable schema failure in a card core."""

    card_id: str
    path: str
    message: str


def load_card_core_v1_schema() -> dict[str, Any]:
    """Load the packaged, versioned JSON Schema candidate."""
    resource = files("skcoord").joinpath(_SCHEMA_RESOURCE)
    return json.loads(resource.read_text(encoding="utf-8"))


CARD_CORE_V1_VALIDATOR = Draft202012Validator(
    load_card_core_v1_schema(),
    format_checker=FormatChecker(),
)


def validate_card_core_v1(core: Mapping[str, Any]) -> list[CoreValidationFailure]:
    """Return stable, attributable tier-one failures for one core mapping."""
    raw_id = core.get("id", "<unknown>")
    card_id = raw_id if isinstance(raw_id, str) else repr(raw_id)
    failures: list[CoreValidationFailure] = []
    for error in sorted(
        CARD_CORE_V1_VALIDATOR.iter_errors(dict(core)),
        key=lambda item: (tuple(str(part) for part in item.absolute_path), item.message),
    ):
        path = ".".join(str(part) for part in error.absolute_path) or "$"
        failures.append(CoreValidationFailure(card_id, path, error.message))
    return failures


def validate_card_core_files(paths: Iterable[Path]) -> list[CoreValidationFailure]:
    """Validate core files without folding lifecycle or evidence events."""
    failures: list[CoreValidationFailure] = []
    for path in sorted(paths):
        fallback_id = path.parent.name
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            failures.append(CoreValidationFailure(fallback_id, "$", f"unreadable JSON: {exc}"))
            continue
        if not isinstance(value, dict):
            failures.append(CoreValidationFailure(fallback_id, "$", "core must be a JSON object"))
            continue
        failures.extend(validate_card_core_v1(value))
    return failures


def validate_card_store(home: Path) -> tuple[int, list[CoreValidationFailure]]:
    """Validate every present ``cards/*/core.json`` and return count + failures."""
    paths = list((home.expanduser() / "cards").glob("*/core.json"))
    return len(paths), validate_card_core_files(paths)


def _main() -> int:
    parser = argparse.ArgumentParser(description="Validate CardStore cores against card-core.v1")
    parser.add_argument("home", type=Path, help="SKCapstone home containing cards/")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    checked, failures = validate_card_store(args.home)
    payload = {
        "schema": "https://skworld.io/schemas/card-core.v1.json",
        "checked": checked,
        "valid": checked - len({failure.card_id for failure in failures}),
        "invalid": len({failure.card_id for failure in failures}),
        "failures": [failure.__dict__ for failure in failures],
    }
    if args.as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"checked={payload['checked']} valid={payload['valid']} invalid={payload['invalid']}")
        for failure in failures:
            print(f"{failure.card_id}: {failure.path}: {failure.message}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
