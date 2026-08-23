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
import stat
import uuid
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
    path = Path(path)
    directory = path.parent
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise RuntimeError("safe atomic writes require O_NOFOLLOW support")
    if directory.is_symlink() or path.name in {"", ".", ".."}:
        raise ValueError("atomic write destination is unsafe")
    try:
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | no_follow)
    except OSError as exc:
        raise ValueError("atomic write parent is unsafe") from exc
    directory_stat = os.fstat(directory_fd)
    if not stat.S_ISDIR(directory_stat.st_mode):
        os.close(directory_fd)
        raise ValueError("atomic write parent is unsafe")
    try:
        existing = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        existing = None
    if existing is not None and (
        stat.S_ISLNK(existing.st_mode)
        or not stat.S_ISREG(existing.st_mode)
        or existing.st_nlink != 1
    ):
        os.close(directory_fd)
        raise ValueError("atomic write destination is unsafe")
    payload = text.encode(encoding)
    temp_name = f".{path.name}.{uuid.uuid4().hex}.tmp"
    fd = -1
    try:
        fd = os.open(
            temp_name,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | no_follow,
            0o600,
            dir_fd=directory_fd,
        )
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise OSError("atomic write made no forward progress")
            offset += written
        os.fsync(fd)
        temporary = os.stat(temp_name, dir_fd=directory_fd, follow_symlinks=False)
        opened = os.fstat(fd)
        if (
            stat.S_ISLNK(temporary.st_mode)
            or not stat.S_ISREG(temporary.st_mode)
            or temporary.st_nlink != 1
            or (temporary.st_dev, temporary.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise ValueError("atomic write temporary destination is unsafe")
        try:
            current = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if current is not None and (
            stat.S_ISLNK(current.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
        ):
            raise ValueError("atomic write destination is unsafe")
        os.replace(temp_name, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temp_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(directory_fd)
