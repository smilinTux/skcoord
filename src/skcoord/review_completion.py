"""Single fail-closed gate for governed review completion."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

_REVIEW_TITLE_RE = re.compile(r"\[RE(?:RE)?VIEW(?:\]|-)", re.IGNORECASE)
_REQUIRED_CI = frozenset(
    {
        "ci_check_docs",
        "ci_check_gitleaks",
        "ci_check_lint",
        "ci_check_shim_imports",
        "ci_check_python311",
        "ci_check_python312",
    }
)


def _rows(path: Path) -> Iterable[dict[str, Any]]:
    if not path.is_dir():
        return
    for event_file in sorted(path.glob("*.jsonl")):
        try:
            with event_file.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(row, dict):
                        yield row
        except OSError:
            continue


def _card_rows(home: Path, card_id: str) -> Iterable[dict[str, Any]]:
    yield from (
        row
        for row in _rows(home / "coordination" / "card_events")
        if row.get("card_id") == card_id
    )
    yield from _rows(home / "cards" / card_id / "events")


def validate_governed_review_completion(home: Path, card_id: str) -> None:
    """Require exact PASS and the complete exact-SUCCESS CI set for reviews."""
    root = Path(home).expanduser()
    core_path = root / "cards" / card_id / "core.json"
    try:
        core = json.loads(core_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError(f"CardStore card {card_id} has no readable core") from exc
    if not _REVIEW_TITLE_RE.search(str(core.get("title") or "")):
        return

    latest: dict[str, tuple[str, str]] = {}
    for row in _card_rows(root, card_id):
        if row.get("action") != "link":
            continue
        key = str(row.get("link_key") or row.get("key") or "")
        value = str(row.get("link_value") or row.get("value") or "")
        stamp = str(row.get("ts") or "")
        current = latest.get(key)
        if current is None or stamp >= current[0]:
            latest[key] = (stamp, value)

    verdict = latest.get("verdict", ("", ""))[1]
    if verdict != "PASS":
        raise ValueError(
            f"governed review card {card_id} requires canonical verdict PASS"
        )
    unsuccessful = sorted(
        key for key in _REQUIRED_CI if latest.get(key, ("", ""))[1] != "SUCCESS"
    )
    if unsuccessful:
        raise ValueError(
            f"governed review card {card_id} has required checks that are not "
            f"exact SUCCESS: {', '.join(unsuccessful)}"
        )

