"""The diff a permission prompt shows for ``edit`` and ``write``.

It is what the call would change, capped, and built only from text the session
already holds: a file the session never read is not read for the preview
either, so the prompt (and the session log that records it) cannot become the
way that file's content gets out.
"""

from __future__ import annotations

import sys
from pathlib import Path

from quickcode.tools.base import ReadRegistry, ToolCtx
from quickcode.tools.fs import diffpreview
from quickcode.tools.registry import default_registry


def ctx(root: Path) -> ToolCtx:
    return ToolCtx(cwd=root, read_registry=ReadRegistry(), platform=sys.platform, extra={})


def seen(c: ToolCtx, path: Path) -> None:
    c.read_registry.record(str(path), path.stat().st_mtime)


def diff(tool: str, c: ToolCtx, **args) -> list[str]:
    t = default_registry().get(tool)
    return t.render_diff(t.Input(**args), c).splitlines()


def test_an_edit_to_a_file_the_session_read_shows_it_in_context(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    c = ctx(tmp_path)
    seen(c, f)
    lines = diff("edit", c, file_path="a.py", old_string="three", new_string="THREE")
    assert lines[:2] == [f"--- {f}", f"+++ {f}"]
    assert "@@ -1,4 +1,4 @@" in lines
    assert [" two", "-three", "+THREE", " four"] == lines[-4:]


def test_replace_all_shows_every_occurrence(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\ny = 2\nx = 1\n", encoding="utf-8")
    c = ctx(tmp_path)
    seen(c, f)
    lines = diff("edit", c, file_path=str(f), old_string="x = 1", new_string="x = 3",
                 replace_all=True)
    assert lines.count("-x = 1") == 2 and lines.count("+x = 3") == 2


def test_a_crlf_file_is_diffed_by_line_not_by_line_ending(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"alpha\r\nbeta\r\n")
    c = ctx(tmp_path)
    seen(c, f)
    lines = diff("edit", c, file_path=str(f), old_string="beta", new_string="gamma")
    assert lines[-3:] == [" alpha", "-beta", "+gamma"]


def test_a_file_the_session_never_read_is_not_read_for_the_preview(tmp_path):
    f = tmp_path / ".env"
    f.write_text("API_KEY=sk-secret\nDEBUG=0\n", encoding="utf-8")
    lines = diff("edit", ctx(tmp_path), file_path=str(f), old_string="DEBUG=0",
                 new_string="DEBUG=1")
    assert not any("sk-secret" in line for line in lines)
    assert lines[:2] == [f"--- {f} (old_string)", f"+++ {f} (new_string)"]
    assert "-DEBUG=0" in lines and "+DEBUG=1" in lines


def test_an_edit_whose_text_is_not_there_shows_the_call_itself(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("one\n", encoding="utf-8")
    c = ctx(tmp_path)
    seen(c, f)
    lines = diff("edit", c, file_path=str(f), old_string="missing", new_string="new")
    assert lines[0].endswith("(old_string)")
    assert "-missing" in lines and "+new" in lines


def test_a_new_file_shows_its_head(tmp_path):
    lines = diff("write", ctx(tmp_path), file_path="new.txt", content="hello\nworld\n")
    assert lines[:2] == ["--- /dev/null", f"+++ {tmp_path / 'new.txt'}"]
    assert lines[-2:] == ["+hello", "+world"]


def test_overwriting_a_file_the_session_read_diffs_against_it(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("keep\nold\n", encoding="utf-8")
    c = ctx(tmp_path)
    seen(c, f)
    lines = diff("write", c, file_path=str(f), content="keep\nnew\n")
    assert lines[-3:] == [" keep", "-old", "+new"]


def test_overwriting_a_file_nobody_read_does_not_show_what_is_in_it(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("private\n", encoding="utf-8")
    lines = diff("write", ctx(tmp_path), file_path=str(f), content="public\n")
    assert not any("private" in line for line in lines)
    assert lines[0] == f"--- {f} (not read in this session)"


def test_a_large_diff_is_capped_with_a_marker(tmp_path):
    content = "".join(f"line {i}\n" for i in range(1000))
    lines = diff("write", ctx(tmp_path), file_path="big.txt", content=content)
    assert len(lines) == diffpreview.MAX_LINES + 1
    assert lines[-1].startswith("… ") and "more diff lines not shown" in lines[-1]


def test_a_long_line_is_cut(tmp_path):
    lines = diff("write", ctx(tmp_path), file_path="min.js", content="x" * 10_000)
    assert len(lines[-1]) == diffpreview.MAX_LINE_CHARS + 1 and lines[-1].endswith("…")


def test_tools_that_change_no_file_have_no_diff(tmp_path):
    bash = default_registry().get("bash")
    assert bash.render_diff(bash.Input(command="make"), ctx(tmp_path)) == ""
