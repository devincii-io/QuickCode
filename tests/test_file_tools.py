"""read, edit and write against the files real projects actually contain.

The happy path -- an LF, UTF-8, ASCII file -- was the only one these tools had
ever been tested on, and it is the one where every shortcut works. These are
the others: CRLF, byte-order marks, UTF-16, legacy code pages, binary files,
files too large to hold, and files that changed underneath the agent.

Every assertion that matters is about the bytes on disk afterwards, because
that is where the damage used to land: a transcript that shows the right text
is no comfort when the file behind it was rewritten.
"""

from __future__ import annotations

import codecs
import os
from pathlib import Path

import pytest

from quickcode.tools import read as read_module
from quickcode.tools.base import ReadRegistry, ToolCtx
from quickcode.tools.edit import EditTool
from quickcode.tools.read import ReadTool
from quickcode.tools.write import WriteTool


@pytest.fixture
def ctx(tmp_path: Path) -> ToolCtx:
    return ToolCtx(cwd=tmp_path, read_registry=ReadRegistry())


async def read(ctx: ToolCtx, path: Path, **kw):
    return await ReadTool().run(ReadTool.Input(file_path=str(path), **kw), ctx)


async def edit(ctx: ToolCtx, path: Path, old: str, new: str, **kw):
    return await EditTool().run(
        EditTool.Input(file_path=str(path), old_string=old, new_string=new, **kw), ctx
    )


async def write(ctx: ToolCtx, path: Path, content: str):
    return await WriteTool().run(WriteTool.Input(file_path=str(path), content=content), ctx)


def shown(body: str) -> list[str]:
    """The text of each numbered line, without the gutter."""
    return [line.split("→", 1)[1] for line in body.split("\n") if "→" in line]


def backdate(path: Path, seconds: float = 100) -> None:
    st = path.stat()
    os.utime(path, (st.st_atime - seconds, st.st_mtime - seconds))


# ---- line endings ---------------------------------------------------------


async def test_a_crlf_file_reads_without_carriage_returns(ctx, tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes(b"one\r\ntwo\r\n")

    result = await read(ctx, path)

    assert shown(result.content) == ["one", "two"]


async def test_editing_one_line_of_a_crlf_file_changes_only_that_line(ctx, tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    await read(ctx, path)

    result = await edit(ctx, path, "two", "TWO")

    assert not result.is_error, result.content
    assert path.read_bytes() == b"one\r\nTWO\r\nthree\r\n"


async def test_a_multi_line_edit_written_with_lf_matches_a_crlf_file(ctx, tmp_path):
    """The model only ever sees ``\\n`` -- read strips the ``\\r`` -- so an
    old_string spanning lines has to be found in a CRLF file anyway."""
    path = tmp_path / "a.txt"
    path.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    await read(ctx, path)

    result = await edit(ctx, path, "one\ntwo\n", "ONE\ninserted\ntwo\n")

    assert not result.is_error, result.content
    assert path.read_bytes() == b"ONE\r\ninserted\r\ntwo\r\nthree\r\n"


async def test_an_lf_file_stays_lf_byte_for_byte(ctx, tmp_path):
    """No newline translation on the way out: on Windows ``write_text`` turned
    every LF of the file into CRLF, not just the edited one."""
    path = tmp_path / "a.txt"
    path.write_bytes(b"one\ntwo\n")
    await read(ctx, path)

    await edit(ctx, path, "one\n", "one\nhalf\n")

    assert path.read_bytes() == b"one\nhalf\ntwo\n"


async def test_overwriting_a_crlf_file_keeps_its_line_endings(ctx, tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes(b"old\r\n")
    await read(ctx, path)

    result = await write(ctx, path, "new\nlines\n")

    assert not result.is_error, result.content
    assert path.read_bytes() == b"new\r\nlines\r\n"


async def test_a_new_file_is_written_exactly_as_given(ctx, tmp_path):
    path = tmp_path / "deep" / "er" / "new.txt"

    result = await write(ctx, path, "a\nb\n")

    assert not result.is_error, result.content
    assert path.read_bytes() == b"a\nb\n"


async def test_a_form_feed_does_not_split_a_line(ctx, tmp_path):
    """``str.splitlines`` splits on \\x0c, U+2028 and friends; ripgrep and
    every editor do not. Line numbers must agree with grep's."""
    path = tmp_path / "a.py"
    path.write_text("a = 1\n\x0c\nb = 2  #   in a comment\nc = 3\n", encoding="utf-8")

    result = await read(ctx, path)

    lines = result.content.split("\n")
    assert lines[3].startswith("     4→c = 3")


# ---- encodings -----------------------------------------------------------


async def test_a_byte_order_mark_is_not_shown_and_survives_an_edit(ctx, tmp_path):
    path = tmp_path / "bom.txt"
    path.write_bytes(codecs.BOM_UTF8 + b"hello\n")

    result = await read(ctx, path)
    assert shown(result.content) == ["hello"]

    await edit(ctx, path, "hello", "bye")
    assert path.read_bytes() == codecs.BOM_UTF8 + b"bye\n"


async def test_a_utf16_file_reads_as_text_and_stays_utf16(ctx, tmp_path):
    """What Windows PowerShell 5's ``Out-File`` and ``>`` produce by default."""
    path = tmp_path / "ps.txt"
    path.write_bytes(codecs.BOM_UTF16_LE + "hi there\r\nline2\r\n".encode("utf-16-le"))

    result = await read(ctx, path)
    assert shown(result.content) == ["hi there", "line2"]

    changed = await edit(ctx, path, "there", "you")
    assert not changed.is_error, changed.content
    assert path.read_bytes() == codecs.BOM_UTF16_LE + "hi you\r\nline2\r\n".encode("utf-16-le")


async def test_a_legacy_code_page_file_reads_correctly_and_says_what_it_is(ctx, tmp_path):
    path = tmp_path / "legacy.txt"
    path.write_bytes("Größe = 1\n".encode("cp1252"))

    result = await read(ctx, path)

    assert shown(result.content)[0] == "Größe = 1"
    assert "cp1252" in result.content


async def test_editing_a_legacy_file_does_not_destroy_the_rest_of_it(ctx, tmp_path):
    """The old edit decoded with replacement and wrote UTF-8 back: every
    non-ASCII byte in the file became U+FFFD on disk, wherever the edit was."""
    path = tmp_path / "legacy.txt"
    path.write_bytes("Größe = 1\n".encode("cp1252"))
    await read(ctx, path)

    result = await edit(ctx, path, "= 1", "= 2")

    assert not result.is_error, result.content
    assert path.read_bytes() == "Größe = 2\n".encode("cp1252")


async def test_a_character_the_encoding_cannot_hold_is_refused_not_mangled(ctx, tmp_path):
    path = tmp_path / "legacy.txt"
    original = "Größe = 1\n".encode("cp1252")
    path.write_bytes(original)
    await read(ctx, path)

    result = await edit(ctx, path, "= 1", "→ 2")

    assert result.is_error
    assert "cp1252" in result.content and "U+2192" in result.content
    assert path.read_bytes() == original


# ---- binary and large files ---------------------------------------------


async def test_a_binary_file_is_refused_rather_than_dumped(ctx, tmp_path):
    path = tmp_path / "image.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(256)) * 4)

    result = await read(ctx, path)

    assert result.is_error
    assert "binary" in result.content
    assert "\x00" not in result.content


async def test_a_file_longer_than_the_window_says_where_to_continue(ctx, tmp_path):
    """Short lines never reached the character cap, so a 5000-line file came
    back as its first 2000 lines with nothing saying there were more."""
    path = tmp_path / "long.txt"
    path.write_text("".join(f"line {n}\n" for n in range(1, 5001)), encoding="utf-8")

    result = await read(ctx, path)

    assert shown(result.content)[-1] == "line 2000"
    assert 'total="5000"' in result.content
    assert "offset=2001" in result.content


async def test_the_character_cap_points_at_the_first_line_it_did_not_show(
    ctx, tmp_path, monkeypatch
):
    """The cap used to cut mid-line and then point at the end of the *window*,
    so following its hint skipped every line between the two."""
    monkeypatch.setattr(read_module, "MAX_OUTPUT_CHARS", 500)
    path = tmp_path / "wide.txt"
    path.write_text("".join(f"{n:04d} " + "x" * 90 + "\n" for n in range(1, 101)), encoding="utf-8")

    result = await read(ctx, path)
    last = int(shown(result.content)[-1][:4])
    assert last < 100
    assert f"offset={last + 1}" in result.content

    following = await read(ctx, path, offset=last + 1)
    assert shown(following.content)[0].startswith(f"{last + 1:04d}")


async def test_a_file_too_large_to_hold_is_read_as_a_window(ctx, tmp_path, monkeypatch):
    """A multi-gigabyte log used to be read into memory whole to show 2000
    lines of it."""
    monkeypatch.setattr(read_module, "WHOLE_FILE_BYTES", 1000)
    path = tmp_path / "huge.log"
    path.write_bytes(b"".join(b"entry %d\r\n" % n for n in range(1, 1001)))

    result = await read(ctx, path, offset=500, limit=3)

    assert shown(result.content) == ["entry 500", "entry 501", "entry 502"]
    assert "offset=503" in result.content


async def test_reading_past_the_end_says_how_long_the_file_is(ctx, tmp_path):
    path = tmp_path / "short.txt"
    path.write_text("a\nb\n", encoding="utf-8")

    result = await read(ctx, path, offset=10)

    assert result.is_error
    assert "2 lines" in result.content


async def test_an_empty_file_says_so(ctx, tmp_path):
    path = tmp_path / "empty.txt"
    path.write_bytes(b"")

    result = await read(ctx, path)

    assert not result.is_error
    assert "empty" in result.content


# ---- edit: which occurrence ----------------------------------------------


async def test_an_ambiguous_old_string_names_the_lines_it_matched(ctx, tmp_path):
    path = tmp_path / "a.py"
    path.write_text("x = 1\ny = 2\nx = 1\n", encoding="utf-8")
    await read(ctx, path)

    result = await edit(ctx, path, "x = 1", "x = 3")

    assert result.is_error
    assert "2 locations" in result.content
    assert "1, 3" in result.content


async def test_an_edit_that_changes_nothing_is_refused(ctx, tmp_path):
    path = tmp_path / "a.py"
    path.write_text("x = 1\n", encoding="utf-8")
    await read(ctx, path)

    result = await edit(ctx, path, "x = 1", "x = 1")

    assert result.is_error
    assert "identical" in result.content


# ---- the file changed underneath ---------------------------------------


async def test_an_edit_after_someone_else_changed_the_file_is_refused(ctx, tmp_path):
    path = tmp_path / "a.py"
    path.write_text("x = 1\n", encoding="utf-8")
    await read(ctx, path)
    path.write_text("x = 1\ny = 2\n", encoding="utf-8")
    backdate(path)

    result = await edit(ctx, path, "x = 1", "x = 3")

    assert result.is_error
    assert "changed on disk" in result.content


async def test_a_change_inside_one_mtime_tick_is_still_caught(ctx, tmp_path):
    """FAT and some network shares count mtime in seconds, so a rewrite in the
    same second as the read left the timestamp exactly as recorded."""
    path = tmp_path / "a.py"
    path.write_text("x = 1\n", encoding="utf-8")
    await read(ctx, path)
    before = path.stat()
    path.write_text("x = 9\n", encoding="utf-8")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

    result = await edit(ctx, path, "x = 9", "x = 3")

    assert result.is_error
    assert "changed on disk" in result.content


async def test_a_file_that_was_only_touched_can_still_be_edited(ctx, tmp_path):
    """A formatter that rewrote the same bytes, or a ``touch``, is not a
    change worth another read."""
    path = tmp_path / "a.py"
    path.write_text("x = 1\n", encoding="utf-8")
    await read(ctx, path)
    backdate(path)

    result = await edit(ctx, path, "x = 1", "x = 3")

    assert not result.is_error, result.content


async def test_a_write_after_someone_else_changed_the_file_is_refused(ctx, tmp_path):
    """write only checked that the file had been read at some point. A user's
    edit made after that read was overwritten without a word."""
    path = tmp_path / "notes.md"
    path.write_text("mine\n", encoding="utf-8")
    await read(ctx, path)
    path.write_text("the user's edit\n", encoding="utf-8")
    backdate(path)

    result = await write(ctx, path, "clobber\n")

    assert result.is_error
    assert "changed on disk" in result.content
    assert path.read_text(encoding="utf-8") == "the user's edit\n"


async def test_the_same_file_by_another_spelling_counts_as_read(ctx, tmp_path):
    (tmp_path / "sub").mkdir()
    path = tmp_path / "a.py"
    path.write_text("x = 1\n", encoding="utf-8")
    await read(ctx, tmp_path / "sub" / ".." / "a.py")

    result = await edit(ctx, path, "x = 1", "x = 3")

    assert not result.is_error, result.content


async def test_overwriting_a_utf16_file_keeps_it_utf16(ctx, tmp_path):
    path = tmp_path / "ps.txt"
    path.write_bytes(codecs.BOM_UTF16_LE + "old\r\n".encode("utf-16-le"))
    await read(ctx, path)

    await write(ctx, path, "new\n")

    assert path.read_bytes() == codecs.BOM_UTF16_LE + "new\r\n".encode("utf-16-le")
