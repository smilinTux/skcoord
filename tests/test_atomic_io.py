"""Descriptor-lifecycle tests for crash-safe coordination writes."""

from __future__ import annotations

import errno
import os

import pytest

import skcoord.atomic_io as atomic_io
from skcoord.atomic_io import atomic_write_text


def _assert_closed(descriptor: int) -> None:
    """Require a closed descriptor instead of inferring it from file contents."""
    with pytest.raises(OSError) as raised:
        os.fstat(descriptor)
    assert raised.value.errno == errno.EBADF


def test_atomic_write_closes_temp_descriptor_after_success(
    tmp_path, monkeypatch
) -> None:
    """A successful replacement must close its temporary descriptor."""
    target = tmp_path / "state.json"
    original_open = os.open
    descriptors: dict[str, int] = {}

    def recording_open(name, flags, mode=0o777, *, dir_fd=None):
        descriptor = original_open(name, flags, mode, dir_fd=dir_fd)
        if isinstance(name, str) and name.startswith(f".{target.name}."):
            descriptors["temp"] = descriptor
        return descriptor

    monkeypatch.setattr(atomic_io.os, "open", recording_open)

    atomic_write_text(target, '{"revision": 1}\n')

    assert target.read_text(encoding="utf-8") == '{"revision": 1}\n'
    _assert_closed(descriptors["temp"])


def test_atomic_write_closes_descriptors_when_replace_fails(
    tmp_path, monkeypatch
) -> None:
    """A failed replacement closes both temporary and directory descriptors."""
    target = tmp_path / "state.json"
    original_open = os.open
    descriptors: dict[str, int] = {}

    def recording_open(name, flags, mode=0o777, *, dir_fd=None):
        descriptor = original_open(name, flags, mode, dir_fd=dir_fd)
        if name == target.parent:
            descriptors["directory"] = descriptor
        elif isinstance(name, str) and name.startswith(f".{target.name}."):
            descriptors["temp"] = descriptor
        return descriptor

    monkeypatch.setattr(atomic_io.os, "open", recording_open)
    monkeypatch.setattr(
        atomic_io.os,
        "replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        atomic_write_text(target, '{"revision": 1}\n')

    _assert_closed(descriptors["temp"])
    _assert_closed(descriptors["directory"])
    assert not list(tmp_path.glob(f".{target.name}.*.tmp"))
