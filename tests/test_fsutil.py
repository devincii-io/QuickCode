from __future__ import annotations

import pytest

from quickcode.fsutil import atomic_write_bytes, atomic_write_text


def test_a_write_replaces_the_file_and_leaves_nothing_beside_it(tmp_path):
    target = tmp_path / "state.json"
    target.write_text("old", encoding="utf-8")

    atomic_write_text(target, "new\n", newline="\n")
    atomic_write_bytes(tmp_path / "raw.bin", b"\x00\x01", fsync=True)

    assert target.read_bytes() == b"new\n"
    assert (tmp_path / "raw.bin").read_bytes() == b"\x00\x01"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["raw.bin", "state.json"]


def test_a_failed_write_keeps_the_old_file_and_cleans_up(tmp_path):
    target = tmp_path / "state.json"
    target.write_text("old", encoding="utf-8")

    with pytest.raises(UnicodeEncodeError):
        atomic_write_text(target, "café", encoding="ascii")

    assert target.read_text(encoding="utf-8") == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]
