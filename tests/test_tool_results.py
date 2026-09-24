"""What a tool hands *back* to the model.

``test_tool_previews.py`` guards the string shown to the user before a call
runs; this guards the string the model reads after it. The claim worth pinning
is that structured results are splittable back into fields: the old
``path:line:text`` rendering was not merely verbose, it was wrong -- a Windows
drive letter puts a colon in the first field and nothing downstream can
recover the path.

The second claim is consistency. ``grep`` has two backends, and if they
disagree about the shape of a result then the format the model sees depends on
whether ripgrep happens to be installed on the machine.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quickcode.tools import grep as grep_module
from quickcode.tools.base import ReadRegistry, ToolCtx
from quickcode.tools.command import _json_for_model
from quickcode.tools.glob import GlobTool
from quickcode.tools.grep import GrepTool

BACKSLASH = chr(92)


def ctx(tmp_path: Path) -> ToolCtx:
    return ToolCtx(cwd=tmp_path, read_registry=ReadRegistry())


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "a.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("run()\nrun()\n", encoding="utf-8")
    return tmp_path


async def grep(tree: Path, **kwargs) -> str:
    result = await GrepTool().run(GrepTool.Input(**kwargs), ctx(tree))
    assert not result.is_error, result.content
    return result.content


def rows(body: str) -> list[str]:
    """The indented data rows of a TOON block."""
    return [line.strip() for line in body.splitlines() if line.startswith("  ")]


def fake_rg(monkeypatch, stdout: str, code: int = 0) -> list[list[str]]:
    """Pretend ripgrep is installed and answers with ``stdout``."""
    calls: list[list[str]] = []

    class _Proc:
        returncode = code
        stdout = b""
        stderr = b""

    def run(args, **_kw):
        calls.append(args)
        proc = _Proc()
        proc.stdout = stdout.encode("utf-8")
        return proc

    monkeypatch.setattr(grep_module.shutil, "which", lambda _name: "rg")
    monkeypatch.setattr(grep_module.subproc, "run", run)
    return calls


def rg_json(*records: tuple[str, int, str]) -> str:
    """ripgrep's ``--json`` event stream for these matches."""
    lines = ['{"type":"begin","data":{"path":{"text":"x"}}}']
    for path, line, text in records:
        lines.append(json.dumps({
            "type": "match",
            "data": {
                "path": {"text": path},
                "lines": {"text": text + "\n"},
                "line_number": line,
            },
        }))
    return "\n".join(lines) + "\n"


# ---- grep: the shape ----


async def test_grep_content_declares_its_fields_once_and_counts_its_rows(tree):
    body = await grep(tree, pattern="run", output_mode="content")

    lines = body.splitlines()
    assert lines[0] == "```toon"
    assert lines[1] == "matches[3]{path,line,text}:"
    assert len(rows(body)) == 3


async def test_grep_count_and_files_modes_say_how_many_without_a_table(tree):
    """These two are plain lines on purpose.

    A bare path, and a count that is digits after the last colon, both come
    apart correctly already -- so a TOON table bought nothing here and cost
    10-30% more tokens. The count marker is the half that was worth keeping.
    """
    counted = await grep(tree, pattern="run", output_mode="count")
    listed = await grep(tree, pattern="run", output_mode="files_with_matches")

    assert counted.splitlines()[0] == '<counts count="2"/>'
    assert [line.rsplit(":", 1)[1] for line in counted.splitlines()[1:]] == ["1", "2"]

    assert listed.splitlines()[0] == '<files count="2"/>'
    assert len(listed.splitlines()) == 3


async def test_a_matched_path_survives_as_one_field(tree):
    """The path column is compared against the path on disk, character for
    character. Under ``path:line:text`` a drive letter broke this."""
    body = await grep(tree, pattern="return", output_mode="content")

    path = rows(body)[0].split(",")[0]
    assert path == str(tree / "a.py").replace(BACKSLASH, "/")


def test_a_windows_path_is_still_one_field_when_the_first_colon_is_a_drive():
    """Pinned without a filesystem, so it holds on a machine with no C: drive."""
    drive = "C:/src/mod.py"
    body = grep_module._emit("matches", [{"path": drive, "line": 12, "text": "x"}], 1)

    assert rows(body)[0].split(",") == [drive, "12", "x"]
    assert f"{drive}:12:x".split(":")[0] == "C"  # what this replaces


async def test_a_matched_line_containing_the_delimiter_is_quoted(tmp_path):
    (tmp_path / "c.py").write_text("call(a, b)\n", encoding="utf-8")
    body = await grep(tmp_path, pattern="call", output_mode="content")

    assert rows(body)[0].endswith('"call(a, b)"')


async def test_a_backslash_in_matched_text_is_no_longer_rewritten(tmp_path):
    r"""Path normalisation used to run over the whole rendered line, so a
    ``\n`` in the source came back as ``/n``."""
    (tmp_path / "d.py").write_text('sep = "' + BACKSLASH + BACKSLASH + '"\n', encoding="utf-8")
    body = await grep(tmp_path, pattern="sep", output_mode="content")

    assert BACKSLASH in body


@pytest.mark.parametrize("backend", ["ripgrep", "fallback"])
async def test_the_declared_count_and_the_truncation_marker_agree(tmp_path, monkeypatch, backend):
    (tmp_path / "e.py").write_text("hit\nhit\nhit\n", encoding="utf-8")
    if backend == "ripgrep":
        fake_rg(monkeypatch, rg_json(*[("e.py", line, "hit") for line in (1, 2, 3)]))
    else:
        monkeypatch.setattr(grep_module.shutil, "which", lambda _name: None)
    body = await grep(tmp_path, pattern="hit", output_mode="content", head_limit=1)

    assert body.splitlines()[1].startswith("matches[1]{")
    assert len(rows(body)) == 1
    # ripgrep scans all matches; the fallback may stop before visiting every file.
    total = "3" if backend == "ripgrep" else "3+"
    assert f'<truncated shown="1" total="{total}"' in body


def test_capping_by_characters_drops_rows_so_the_header_stays_honest(monkeypatch):
    """Slicing the encoded string would leave a header promising rows that are
    no longer there -- and the count is the one thing the model can check."""
    monkeypatch.setattr(grep_module, "MAX_OUTPUT_CHARS", 200)
    many = [{"path": f"f{n}.py", "line": n, "text": "x" * 40} for n in range(50)]

    body = grep_module._emit("matches", many, len(many))

    declared = int(body.splitlines()[1].split("[")[1].split("]")[0])
    assert declared == len(rows(body)) < 50
    assert f'total="{len(many)}"' in body


async def test_no_matches_says_so_rather_than_emitting_an_empty_table(tree):
    assert await grep(tree, pattern="nothing-here") == "No matches found."


# ---- grep: the two backends agree ----


async def test_both_search_backends_return_the_same_records(tree, monkeypatch):
    """Whether ripgrep is installed must not change the format of a result."""
    fallback = await grep(tree, pattern="run", output_mode="content")

    fake_rg(monkeypatch, rg_json(
        *[(row.split(",")[0], int(row.split(",")[1]), row.split(",", 2)[2])
          for row in rows(fallback)]
    ))
    with_ripgrep = await grep(tree, pattern="run", output_mode="content")

    assert with_ripgrep == fallback


async def test_the_ripgrep_content_path_asks_for_json(tree, monkeypatch):
    calls = fake_rg(monkeypatch, rg_json(("a.py", 1, "def run():")))
    await grep(tree, pattern="run", output_mode="content")

    assert "--json" in calls[0]


async def test_the_count_and_files_modes_are_read_from_plain_output(tree, monkeypatch):
    """``--json`` only applies to search mode; ripgrep ignores it with -l/-c."""
    calls = fake_rg(monkeypatch, "C:/src/a.py:4\n")
    body = await grep(tree, pattern="run", output_mode="count")

    assert "--json" not in calls[0]
    # The count is digits after the *last* colon, so the drive letter survives.
    line = body.splitlines()[1]
    assert line == "C:/src/a.py:4"
    assert line.rsplit(":", 1) == ["C:/src/a.py", "4"]


async def test_a_ripgrep_that_cannot_speak_json_falls_back_instead_of_lying(
    tree, monkeypatch
):
    """An older ripgrep answering in its own format would otherwise parse to
    zero rows and be reported as "no matches"."""
    fake_rg(monkeypatch, "a.py:1:def run():\n")
    body = await grep(tree, pattern="run", output_mode="content")

    assert body.splitlines()[1] == "matches[3]{path,line,text}:"


async def test_one_unreadable_file_does_not_throw_away_ripgreps_results(tree, monkeypatch):
    """ripgrep exits 2 when any file could not be read, matches or not. That
    used to discard its answer and run the whole search again in Python."""
    calls = fake_rg(monkeypatch, "/p/a.py\n", code=2)
    monkeypatch.setattr(grep_module, "_run_fallback",
                        lambda *a, **k: pytest.fail("fell back to the Python walk"))

    body = await grep(tree, pattern="run")

    assert len(calls) == 1
    assert body.splitlines()[1:] == ["/p/a.py"]


async def test_a_ripgrep_error_with_no_output_still_falls_back(tree, monkeypatch):
    fake_rg(monkeypatch, "", code=2)

    body = await grep(tree, pattern="run")

    assert body.splitlines()[0] == '<files count="2"/>'


# ---- grep: the order is the path order, whoever searched ----


@pytest.mark.parametrize("mode", ["count", "files_with_matches"])
async def test_ripgreps_thread_order_does_not_reach_the_model(tree, monkeypatch, mode):
    """ripgrep searches files on several threads and prints whichever finishes
    first, so the same search answered ``b, a`` on one run and ``a, b`` on the
    next. A result that reorders itself between identical calls reads as a
    change that did not happen."""
    lines = ["/p/z.py", "/p/sub/b.py", "/p/a.py", "/p/sub.py"]
    out = [f"{p}:1" for p in lines] if mode == "count" else lines
    fake_rg(monkeypatch, "\n".join(out) + "\n")

    body = await grep(tree, pattern="run", output_mode=mode)

    paths = [line.rsplit(":", 1)[0] if mode == "count" else line
             for line in body.splitlines()[1:]]
    assert paths == ["/p/a.py", "/p/sub/b.py", "/p/sub.py", "/p/z.py"]


async def test_ripgrep_content_rows_are_ordered_by_path_and_keep_line_order(tree, monkeypatch):
    fake_rg(monkeypatch, rg_json(("/p/b.py", 3, "x"), ("/p/b.py", 9, "y"), ("/p/a.py", 5, "z")))

    body = await grep(tree, pattern="run", output_mode="content")

    assert [r.split(",")[:2] for r in rows(body)] == [
        ["/p/a.py", "5"], ["/p/b.py", "3"], ["/p/b.py", "9"],
    ]


@pytest.mark.parametrize("mode", ["count", "files_with_matches", "content"])
async def test_the_fallback_walks_in_the_same_path_order(tmp_path, monkeypatch, mode):
    for name in ("z.py", "sub/b.py", "a.py", "sub.py", "m/n/o.py", "b.py"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("hit\n", encoding="utf-8")
    monkeypatch.setattr(grep_module.shutil, "which", lambda _name: None)

    body = await grep(tmp_path, pattern="hit", output_mode=mode)

    found = [row.split(",")[0] for row in rows(body)] if mode == "content" else [
        line.rsplit(":", 1)[0] if mode == "count" else line
        for line in body.splitlines()[1:]
    ]
    rel = [p.removeprefix(str(tmp_path).replace(BACKSLASH, "/") + "/") for p in found]
    assert rel == ["a.py", "b.py", "m/n/o.py", "sub/b.py", "sub.py", "z.py"]


@pytest.mark.skipif(not grep_module.shutil.which("rg"), reason="needs ripgrep installed")
async def test_counting_in_one_named_file_still_names_it(tree):
    """``rg -c`` leaves the path off when it was given a single file, so the
    count came back as a row whose *path* was the number."""
    body = await grep(tree, pattern="run", path=str(tree / "b.py"), output_mode="count")

    assert body.splitlines()[1] == str(tree / "b.py").replace(BACKSLASH, "/") + ":2"


async def test_the_fallback_names_the_file_it_counted_in_too(tree, monkeypatch):
    monkeypatch.setattr(grep_module.shutil, "which", lambda _name: None)
    body = await grep(tree, pattern="run", path=str(tree / "b.py"), output_mode="count")

    assert body.splitlines()[1] == str(tree / "b.py").replace(BACKSLASH, "/") + ":2"


# ---- glob ----


async def test_glob_declares_how_many_paths_came_back(tree):
    """Without the count, a listing cut at the 200 cap looks like a listing
    that found exactly 200. The paths themselves need no encoding."""
    result = await GlobTool().run(GlobTool.Input(pattern="*.py"), ctx(tree))

    lines = result.content.splitlines()
    assert lines[0] == '<files count="2"/>'
    assert len(lines) == 3
    assert all(line.endswith(".py") for line in lines[1:])


async def test_glob_with_no_matches_says_so(tree):
    result = await GlobTool().run(GlobTool.Input(pattern="*.rs"), ctx(tree))
    assert result.content == "No files matched."


# ---- authored command tools: the one arbitrary payload ----


def test_a_uniform_record_list_from_a_command_tool_is_encoded_as_a_table():
    payload = [{"host": "a", "up": True}, {"host": "b", "up": False}]

    out = _json_for_model(payload)

    assert out.splitlines()[1] == "[2]{host,up}:"
    assert len(out) < len(json.dumps(payload, indent=2))


@pytest.mark.parametrize("payload", [{"ok": True}, 42, {"msg": 'x, y, "z"' * 20}])
def test_a_payload_the_fence_costs_more_than_it_saves_stays_json(payload):
    """The heuristic is the whole decision, and it does not always pick TOON:
    on a bare scalar, a two-field object or one long quoted string the fence
    is more than the encoding gives back."""
    out = _json_for_model(payload)

    assert "```toon" not in out
    assert json.loads(out) == payload
