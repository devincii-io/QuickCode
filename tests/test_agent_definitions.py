"""Agent definitions on disk: what ``.quickcode/agents/*.md`` may say, and
what happens to a file that says it wrong.

A definition's name becomes the agent id, and the id becomes a file name
under ``.quickcode/artifacts`` and an attribute in the ``<subagent id=...>``
tag the parent reads -- so a name is not free text. A file that cannot be
used is skipped with a warning naming it, never silently and never by
taking the session down.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quickcode.subagents.definitions import load_defs


def _write_def(cwd: Path, filename: str, text: str, *, encoding: str = "utf-8") -> None:
    d = cwd / ".quickcode" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_text(text, encoding=encoding)


@pytest.mark.parametrize("name", [
    "../../outside", "a/b", "a\\b", 'x" status="done', "has space", "@orchestrator2", "",
])
def test_a_definition_whose_name_is_not_a_safe_id_is_refused(tmp_path, name, caplog):
    """The name becomes the agent id, and the id becomes a file name under
    ``.quickcode/artifacts`` and an attribute in the ``<subagent id=...>`` tag
    the parent reads. A project file is enough to set it."""
    _write_def(tmp_path, "evil.md", f"---\nname: {name}\ntools: [read]\n---\nBody.\n")
    defs = load_defs(tmp_path)
    assert name not in defs or name == ""
    if name:
        assert any("evil.md" in r.getMessage() for r in caplog.records)


def test_a_definition_saved_with_a_byte_order_mark_keeps_its_frontmatter(tmp_path):
    # Notepad writes one. The frontmatter used to be read as body text, which
    # dropped the tools allowlist the file asked for.
    _write_def(tmp_path, "reviewer.md",
               "---\nname: reviewer\ntools: [read, grep]\nmode_cap: ask\n---\nReview.\n",
               encoding="utf-8-sig")
    defs = load_defs(tmp_path)
    assert defs["reviewer"].tools == ["read", "grep"]
    assert defs["reviewer"].prompt_body == "Review."


def test_a_definition_that_cannot_be_read_is_reported_not_swallowed(tmp_path, caplog):
    _write_def(tmp_path, "broken.md", "")
    (tmp_path / ".quickcode" / "agents" / "broken.md").write_bytes(b"---\nname: \xff\xfe\n---\n")
    defs = load_defs(tmp_path)
    assert "explore" in defs
    assert any("broken.md" in r.getMessage() for r in caplog.records)
