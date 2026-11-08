"""Read-only, exhaustive validation of CardStore JSONL event files."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class MalformedCardStoreLine:
    """One physical CardStore event-log line that cannot be decoded as JSON.

    ``sha256`` covers the exact line bytes as stored, including its line ending
    when one is present. This makes the report evidence byte-specific without
    copying potentially sensitive event content into output.
    """

    card: str
    file: str
    line: int
    sha256: str
    reason: str

    def as_dict(self) -> dict[str, str | int]:
        return asdict(self)


def _regular_single_link(path: Path) -> bool:
    """Return true only for a non-symlink regular file with one hard link."""
    try:
        details = path.stat(follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(details.st_mode) and details.st_nlink == 1


def find_malformed_cardstore_lines(home: Path) -> list[MalformedCardStoreLine]:
    """Report every malformed non-empty line in CardStore event logs.

    The candidate is deliberately read-only: it opens existing event files
    with ``O_NOFOLLOW``, reads bytes, and never creates, replaces, truncates,
    or appends any store path. Only ``cards/<card>/events/*.jsonl`` belongs to
    this structural CardStore scan; evidence and other stores remain separate.

    Results are ordered by card, file, and physical line number. Unsafe or
    unreadable filesystem entries are ignored rather than traversed. Store
    readers enforce those distinct structural errors separately.
    """
    root = Path(home).expanduser() / "cards"
    try:
        card_dirs = sorted(entry for entry in root.iterdir() if entry.is_dir() and not entry.is_symlink())
    except OSError:
        return []

    findings: list[MalformedCardStoreLine] = []
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    for card_dir in card_dirs:
        events_dir = card_dir / "events"
        if events_dir.is_symlink():
            continue
        try:
            files = sorted(events_dir.glob("*.jsonl"))
        except OSError:
            continue
        for path in files:
            if not _regular_single_link(path):
                continue
            try:
                descriptor = os.open(path, os.O_RDONLY | no_follow)
                try:
                    with os.fdopen(descriptor, "rb") as source:
                        lines = list(source)
                    descriptor = -1
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
            except OSError:
                continue

            relative_file = path.relative_to(root).as_posix()
            for line_number, raw_line in enumerate(lines, start=1):
                if not raw_line.strip():
                    continue
                try:
                    json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    findings.append(
                        MalformedCardStoreLine(
                            card=card_dir.name,
                            file=relative_file,
                            line=line_number,
                            sha256=hashlib.sha256(raw_line).hexdigest(),
                            reason=str(exc),
                        )
                    )
    return findings
