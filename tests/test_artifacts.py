"""Artifacts-to-disk: large subagent reports get offloaded to a file, with
only a short head + path reference passed back to the parent's context."""

from pathlib import Path

from quickcode.subagents.artifacts import (
    ARTIFACT_CHAR_LIMIT,
    ARTIFACT_HEAD_LINES,
    maybe_offload,
    write_artifact,
)


def test_small_report_passes_through_unchanged(tmp_path: Path):
    report = "all good, found 2 issues, fixed both.\n- foo.py: typo\n- bar.py: off-by-one"
    out = maybe_offload(tmp_path, "explore-1", report)
    assert out == report
    # no artifact should have been written for a small report
    assert not (tmp_path / ".quickcode" / "artifacts").exists()


def test_large_report_is_trimmed_and_written_to_disk(tmp_path: Path):
    # Short lines so the head-lines cap (not the char cap) is what's exercised.
    lines = [f"line {i}" for i in range(200)]
    report = "\n".join(lines)
    assert len(report) > ARTIFACT_CHAR_LIMIT
    assert len(lines) > ARTIFACT_HEAD_LINES

    out = maybe_offload(tmp_path, "researcher-3", report)

    assert len(out) < len(report)
    assert "line 0" in out
    assert f"line {ARTIFACT_HEAD_LINES - 1}" in out
    assert "line " + str(ARTIFACT_HEAD_LINES + 5) not in out
    assert "full report" in out
    assert "written to" in out

    artifact_path = tmp_path / ".quickcode" / "artifacts" / "researcher-3.md"
    assert str(artifact_path) in out
    assert artifact_path.exists()
    assert artifact_path.read_text(encoding="utf-8") == report


def test_write_artifact_returns_none_when_parent_is_not_a_dir(tmp_path: Path):
    # cwd is actually a file, so cwd/.quickcode/artifacts can never be created.
    blocked_cwd = tmp_path / "not_a_dir"
    blocked_cwd.write_text("i am a file, not a directory", encoding="utf-8")

    assert write_artifact(blocked_cwd, "agent-1", "some report") is None


def test_maybe_offload_returns_original_when_write_fails(tmp_path: Path):
    blocked_cwd = tmp_path / "not_a_dir"
    blocked_cwd.write_text("i am a file, not a directory", encoding="utf-8")

    lines = [f"line {i}" for i in range(200)]
    report = "\n".join(lines)

    out = maybe_offload(blocked_cwd, "agent-1", report)
    assert out == report
