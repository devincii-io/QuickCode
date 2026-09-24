"""Files QuickCode rewrites in place survive a write that fails halfway.

A full disk fails a write after part of it landed. Written in place, the file
is left truncated: a torn ``config.json`` stops the app from starting at all,
and a torn key file reads as no key. Written beside the target and renamed
over it (``fsutil``), the old file is still there.
"""

from __future__ import annotations

import builtins
import errno
import io
import os
import stat
import sys

import pytest

from quickcode import secrets
from quickcode.config import Config
from quickcode.fsutil import atomic_write_bytes


@pytest.fixture
def disk_fills_up(monkeypatch):
    """Writes to a file whose path contains one of the returned names stop
    halfway with ENOSPC."""
    names: set[str] = set()
    real_open = io.open

    class HalfWritten:
        def __init__(self, fh):
            self.fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.fh.close()
            return False

        def write(self, data):
            self.fh.write(data[: len(data) // 2])
            self.fh.flush()
            raise OSError(errno.ENOSPC, "No space left on device")

        def __getattr__(self, name):
            return getattr(self.fh, name)

    def open_(file, mode="r", *args, **kwargs):
        fh = real_open(file, mode, *args, **kwargs)
        if set(mode) & set("wxa") and any(n in str(file) for n in names):
            return HalfWritten(fh)
        return fh

    monkeypatch.setattr(io, "open", open_)
    monkeypatch.setattr(builtins, "open", open_)
    return names


def test_a_failed_config_save_keeps_the_config_that_was_there(tmp_path, disk_fills_up):
    path = tmp_path / "config.json"
    Config(last_model="kept/model").save(path)

    disk_fills_up.add("config.json")
    with pytest.raises(OSError):
        Config(last_model="new/model").save(path)

    assert Config.load(path).last_model == "kept/model"
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]


def test_a_failed_key_save_keeps_the_key_that_was_there(tmp_path, monkeypatch, disk_fills_up):
    monkeypatch.setattr(secrets, "SECRETS_DIR", tmp_path)
    secrets.save_secret("brave", "old-key")

    disk_fills_up.add("brave.key")
    with pytest.raises(OSError):
        secrets.save_secret("brave", "new-key")

    assert secrets.load_secret("brave") == "old-key"
    assert [p.name for p in tmp_path.iterdir()] == ["brave.key"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_a_private_file_is_private_from_the_moment_it_exists(tmp_path, monkeypatch):
    # Not made private afterwards: between a plain write and a chmod the
    # contents sit readable to every account on the machine.
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
    monkeypatch.setattr(secrets, "SECRETS_DIR", tmp_path)
    old = os.umask(0o022)
    try:
        atomic_write_bytes(tmp_path / "raw.bin", b"x", mode=0o600)
        secrets.save_secret("brave", "k")
    finally:
        os.umask(old)

    for name in ("raw.bin", "brave.key"):
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o600
