"""Crash-safe atomic file writes for the coordination and ITIL stores.

Both stores are flat JSON/Markdown files synced across the fleet via
Syncthing. A plain ``path.write_text`` truncates the live file and then
streams the new bytes: a crash (or a Syncthing read) mid-write leaves a
torn, half-written file that fails to parse and silently drops a task,
agent record, or vote from every derived board view.

``atomic_write_text`` removes that window: it writes the full payload to a
temp file in the same directory, fsyncs it, then ``os.replace``s it over the
target (an atomic rename on POSIX). A crash therefore leaves either the whole
old file or the whole new file, never a partial one. The parent directory is
fsynced last so the rename itself is durable.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

__all__ = ["atomic_write_text"]


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Atomically write ``text`` to ``path`` (tmp file + fsync + os.replace).

    The target is never truncated in place. On any error the temp file is
    removed and the original target is left untouched.

    Args:
        path: Destination file. Its parent directory must already exist.
        text: Full file contents to write.
        encoding: Text encoding for the payload.
    """
    directory = path.parent
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dir_fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
