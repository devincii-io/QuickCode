"""Grep tool: search file contents with a regex.

Searches file contents for a regular expression. Use this instead of a bash
``grep``/``rg`` invocation — it prefers ripgrep when available on PATH and
transparently falls back to a pure-Python walk otherwise, so behavior is
consistent either way. Supports three output modes (files_with_matches,
content, count), an optional glob filter, and case-insensitive matching.

Results reach the model as TOON: one header naming the fields, one row per
hit. That replaced ``path:line:text``, which was not merely verbose but
*wrong* on Windows -- ``C:/src/a.py:12:def f():`` splits to ``C`` on the first
colon and nothing downstream can recover the path. TOON quotes values against
the delimiter instead, and its header carries a row count the model can check
against what it actually read.

The two backends must agree on that shape, or the model gets a different
format depending on whether ripgrep happens to be installed. That is why the
ripgrep content path asks for ``--json`` rather than parsing rg's own
``path:line:text``: the fields arrive already separated, which is the only way
to hand back the same records the Python walk produces.

They must also agree on *which files* are searched, and in what order:

* results come back in path order (ripgrep's are sorted after the fact);
* ``.gitignore`` is honoured by both (``fs/walk.py`` reads it the way
  ripgrep does), and so are the always-pruned directories below;
* hidden files are searched inside the project -- ``.github/workflows`` is
  code -- and skipped outside it, where dot-directories are where credentials
  live;
* files over ``MAX_FILE_BYTES`` and binary files are skipped by both.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

from quickcode import subproc
from quickcode.context import toon
from quickcode.tools.base import PermissionSpec, Tool, ToolCtx, ToolResult
from quickcode.tools.fs import textfile
from quickcode.tools.fs.patterns import PatternError, compile_glob
from quickcode.tools.fs.walk import IGNORED_DIRS, walk_files

# Secret-bearing and QuickCode-private paths, skipped while *walking*. The
# permission gate prompts before a search that names one of these, but a search
# of the whole project names none of them and would otherwise return the
# contents of every one it passed. Naming the file explicitly still searches it
# -- with the prompt -- which is the same shape ``read`` has: reachable, never
# incidental. The list is the permission engine's protected set.
PROTECTED_NAMES = (".git", ".quickcode", ".ssh", ".env")
PROTECTED_GLOBS = tuple(f"!{name}" for name in PROTECTED_NAMES) + ("!.env.*",)
MAX_FILE_BYTES = 5_000_000
MAX_OUTPUT_CHARS = 40_000
# A matched line longer than this is cut to a window around the match. One
# minified bundle used to put a megabyte-long row into the result.
MAX_TEXT_CHARS = 500
RG_TIMEOUT_S = 30

# The TOON key each output mode reports under. The key names what the rows
# are, so a model reading only the header line already knows which mode ran.
KEYS = {"content": "matches", "count": "counts", "files_with_matches": "files"}
HINT = "narrow the pattern or path"


class GrepInput(BaseModel):
    pattern: str = Field(..., description="Regular expression to search for.")
    path: str | None = Field(None, description="File or directory to search in (default: cwd).")
    glob: str | None = Field(None, description='Only search files matching this glob, e.g. "*.py".')
    output_mode: Literal["content", "files_with_matches", "count"] = Field(
        "files_with_matches", description="What to return: matching lines, file paths, or counts."
    )
    context: int | None = Field(
        None, description="Lines of context to show before/after each match (content mode only)."
    )
    ignore_case: bool = Field(False, description="Case-insensitive match.")
    head_limit: int = Field(100, description="Cap the number of result lines/entries returned.")


class GrepTool(Tool[GrepInput]):
    name: ClassVar[str] = "grep"
    description: ClassVar[str] = (
        "Searches file contents for a regular expression pattern. Use this "
        "instead of running grep/rg via Bash. Prefers ripgrep when installed, "
        "falling back to an equivalent pure-Python search otherwise. "
        "output_mode='content' returns a TOON table, matches{path,line,text}, "
        "whose header declares the row count. "
        "output_mode='files_with_matches' (default) returns one path per line "
        "and 'count' returns one path:count per line, each after a marker "
        "giving the number of results. Filter with glob (e.g. '*.py'), narrow "
        "with path, and cap results with head_limit (default 100)."
    )
    is_read_only: ClassVar[bool] = True
    # ``path`` is a filesystem path, so it is gated exactly as ``read`` is:
    # ``output_mode="content"`` returns file contents, which makes an ungated
    # grep a way to read ~/.ssh that the tool asking for the same file by name
    # would have been prompted for.
    permission = PermissionSpec(mutates=False, target_field="path", path_target=True)
    Input = GrepInput

    def render_call(self, input: GrepInput) -> str:  # noqa: A002
        where = f" in {input.path}" if input.path else ""
        return f'⏺ Grep "{input.pattern}"{where}'

    async def run(self, input: GrepInput, ctx: ToolCtx) -> ToolResult:  # noqa: A002
        root = Path(input.path) if input.path else ctx.cwd
        if not root.is_absolute():
            root = ctx.cwd / root

        if not root.exists():
            return ToolResult(content=f"Error: path not found: {root}", is_error=True)

        try:
            rx = re.compile(input.pattern, re.IGNORECASE if input.ignore_case else 0)
        except re.error as exc:
            return ToolResult(content=f"Error: invalid regex {input.pattern!r}: {exc}", is_error=True)
        try:
            include = compile_glob(input.glob, anywhere=True) if input.glob else None
        except PatternError as exc:
            return ToolResult(content=f"Error: {exc}", is_error=True)

        hidden = _inside(root, ctx.cwd)
        # Off the event loop: a search of a large tree is seconds of blocking
        # I/O, and the loop it would block serves every session and socket.
        try:
            body = await asyncio.to_thread(_search, input, root, rx, include, hidden)
        except subprocess.TimeoutExpired:
            return ToolResult(
                content=f"Error: the search took longer than {RG_TIMEOUT_S}s. {HINT}.",
                is_error=True,
            )
        return ToolResult(content=body)


def _inside(root: Path, project: Path) -> bool:
    try:
        return root.resolve().is_relative_to(Path(project).resolve())
    except OSError:
        return False


def _search(input: GrepInput, root: Path, rx: re.Pattern[str],  # noqa: A002
            include: re.Pattern[str] | None, hidden: bool) -> str:
    rg = shutil.which("rg")
    if rg:
        try:
            return _run_ripgrep(rg, input, root, hidden=hidden)
        except subprocess.TimeoutExpired:
            raise
        except Exception:
            pass  # fall through to pure-python fallback
    return _run_fallback(input, root, rx, include, hidden=hidden)


# -- ripgrep ------------------------------------------------------------------


def _run_ripgrep(rg: str, input: GrepInput, root: Path, *, hidden: bool = True) -> str:  # noqa: A002
    # ``--with-filename`` because ``rg -c`` leaves the path off when it is
    # handed a single file, and the count would then be read as the path.
    # ``--no-ignore-global``: the walk cannot see git's core.excludesFile, and
    # a machine-specific ignore list is exactly how the two backends would
    # come to disagree on one user's machine and not on another's.
    args = [rg, "--no-heading", "--line-number", "--color=never", "--with-filename",
            f"--max-filesize={MAX_FILE_BYTES}", "--no-ignore-global"]
    if hidden:
        args.append("--hidden")
    if input.ignore_case:
        args.append("-i")
    if input.glob:
        args += ["--glob", input.glob]
    if root.is_dir():
        pruned = sorted(IGNORED_DIRS.difference(PROTECTED_NAMES))
        for pattern in (*PROTECTED_GLOBS, *(f"!{d}" for d in pruned)):
            args += ["--glob", pattern]
    if input.output_mode == "files_with_matches":
        args.append("-l")
    elif input.output_mode == "count":
        args.append("-c")
    else:
        # ``--json`` only applies to search mode -- with -l or -c ripgrep
        # ignores it -- which is fine, because those two shapes are the ones
        # that survive a split: a bare path, and a path whose count is digits
        # after the last colon.
        args.append("--json")
        if input.context:
            args += ["-C", str(input.context)]
    args += ["--", input.pattern, str(root)]

    # ripgrep anchors ``--glob`` patterns to its working directory, not to the
    # path it searches. Run from anywhere else and ``src/*.ts`` matches nothing
    # -- which it did, from the server's own cwd, until this.
    where = root if root.is_dir() else root.parent
    proc = subproc.run(args, capture_output=True, timeout=RG_TIMEOUT_S, text=False,
                       cwd=str(where))
    # 2 is also what ripgrep exits with when it found matches but could not
    # read some file (a locked file on Windows, a root-owned one elsewhere).
    # Those results are good; only an error with no output at all -- a bad
    # pattern -- sends the search to the Python walk.
    if proc.returncode not in (0, 1) and not proc.stdout.strip():
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace"))

    lines = proc.stdout.decode("utf-8", errors="replace").splitlines()
    if not lines:
        return "No matches found."

    if input.output_mode == "files_with_matches":
        rows: list[dict[str, Any]] = [{"path": _slash(line)} for line in lines]
    elif input.output_mode == "count":
        rows = [_count_row(line) for line in lines]
    else:
        rows = _rows_from_json(lines)
        rx = _python_regex(input)
        for row in rows:
            row["text"] = _clip(row["text"], rx)
    if not rows:
        return "No matches found."

    # ripgrep prints files in whichever order its threads finish them. Sorting
    # here rather than passing ``--sort path`` keeps the search parallel; the
    # sort is stable, so a file's rows keep their line order.
    rows.sort(key=lambda row: path_key(row["path"]))
    return _emit(KEYS[input.output_mode], rows[: max(1, input.head_limit)], len(rows))


def _python_regex(input: GrepInput) -> re.Pattern[str] | None:  # noqa: A002
    try:
        return re.compile(input.pattern, re.IGNORECASE if input.ignore_case else 0)
    except re.error:
        return None


# -- pure Python ----------------------------------------------------------------


def _run_fallback(input: GrepInput, root: Path, rx: re.Pattern[str],  # noqa: A002
                  include: re.Pattern[str] | None, *, hidden: bool) -> str:
    files = _iter_files(root, include, hidden)
    limit = max(1, input.head_limit)
    if input.output_mode == "files_with_matches":
        return _collect_files_with_matches(files, rx, limit)
    if input.output_mode == "count":
        return _collect_counts(files, rx, limit)
    return _collect_content(files, rx, limit, max(0, input.context or 0))


def _iter_files(root: Path, include: re.Pattern[str] | None, hidden: bool) -> Iterator[Path]:
    if root.is_file():
        if include is None or include.match(root.name):
            yield root
        return
    for rel, path in walk_files(root, exclude=_excluded, hidden=hidden):
        if include is None or include.match(rel):
            yield path


def _collect_files_with_matches(files: Iterator[Path], rx: re.Pattern[str], limit: int) -> str:
    results = []
    for p in files:
        lines = _read_text_lines(p)
        if lines is None:
            continue
        if any(rx.search(line) for line in lines):
            results.append({"path": _norm(p)})
        if len(results) > limit:
            break
    if not results:
        return "No matches found."
    return _emit("files", results[:limit], len(results), approx=True)


def _collect_counts(files: Iterator[Path], rx: re.Pattern[str], limit: int) -> str:
    results = []
    for p in files:
        lines = _read_text_lines(p)
        if lines is None:
            continue
        n = sum(1 for line in lines if rx.search(line))
        if n:
            results.append({"path": _norm(p), "matches": n})
    if not results:
        return "No matches found."
    return _emit("counts", results[:limit], len(results))


def _collect_content(
    files: Iterator[Path], rx: re.Pattern[str], limit: int, context: int
) -> str:
    rows: list[dict[str, Any]] = []
    total_matches = 0
    for p in files:
        lines = _read_text_lines(p)
        if lines is None:
            continue
        shown_upto = -1  # context windows overlap; ripgrep prints a line once
        for i, line in enumerate(lines):
            if not rx.search(line):
                continue
            total_matches += 1
            if len(rows) >= limit:
                continue
            lo = max(i - context, shown_upto + 1)
            hi = min(len(lines), i + context + 1)
            for j in range(lo, hi):
                rows.append({"path": _norm(p), "line": j + 1, "text": _clip(lines[j], rx)})
            shown_upto = max(shown_upto, hi - 1)
        if total_matches > limit and len(rows) >= limit:
            break
    if not rows:
        return "No matches found."
    return _emit("matches", rows[:limit], max(total_matches, len(rows)), approx=True)


def _excluded(name: str, is_dir: bool) -> bool:
    if name in PROTECTED_NAMES or name.startswith(".env."):
        return True
    return is_dir and name in IGNORED_DIRS


def _read_text_lines(path: Path) -> list[str] | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    try:
        encoding = textfile.detect(data)
    except textfile.NotText:
        return None
    return textfile.split_lines(textfile.decode(data, encoding, errors="replace"))


def _clip(text: str, rx: re.Pattern[str] | None) -> str:
    """A long line cut down to a window around its first match."""
    if len(text) <= MAX_TEXT_CHARS:
        return text
    hit = rx.search(text) if rx is not None else None
    start = max(0, (hit.start() if hit else 0) - MAX_TEXT_CHARS // 4)
    end = start + MAX_TEXT_CHARS
    head = "…" if start else ""
    tail = f"…[{len(text):,} chars]" if end < len(text) else ""
    return f"{head}{text[start:end]}{tail}"


def path_key(path: str) -> tuple[str, ...]:
    """Sort key for a slash-separated path: component by component.

    Plain string order would put ``sub.py`` before ``sub/b.py`` ('.' < '/'),
    which is not the order a directory walk visits them in -- and the
    fallback's walk is what this order has to agree with.
    """
    return tuple(path.split("/"))


def _count_row(line: str) -> dict[str, Any]:
    """``path:count`` from ``rg -c``. The count is digits after the *last*
    colon, so a drive letter in the path cannot be mistaken for it."""
    path, _, n = line.rpartition(":")
    return {"path": _slash(path or line), "matches": int(n) if n.isdigit() else 0}


def _rows_from_json(lines: list[str]) -> list[dict[str, Any]]:
    """``{path,line,text}`` rows from ripgrep's ``--json`` event stream.

    Context lines (from ``-C``) become rows too, exactly as they do in the
    pure-Python walk: the two backends must return the same records.
    """
    rows: list[dict[str, Any]] = []
    events = 0
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or "type" not in event:
            continue
        events += 1
        if event["type"] not in ("match", "context"):
            continue
        data = event.get("data") or {}
        rows.append({
            "path": _slash(_rg_text(data.get("path"))),
            "line": data.get("line_number") or 0,
            "text": _rg_text(data.get("lines")).rstrip("\r\n"),
        })
    if not events:
        # This ripgrep did not answer in JSON. Reporting "no matches" would be
        # a lie, so raise and let ``run`` take the pure-Python path instead.
        raise RuntimeError("ripgrep produced no --json events")
    return rows


def _rg_text(field: Any) -> str:
    """ripgrep sends ``{"text": ...}``, or ``{"bytes": <base64>}`` when the
    value is not valid UTF-8."""
    if not isinstance(field, dict):
        return ""
    if isinstance(field.get("text"), str):
        return field["text"]
    raw = field.get("bytes")
    if isinstance(raw, str):
        return base64.b64decode(raw).decode("utf-8", errors="replace")
    return ""


def _norm(p: Path) -> str:
    return _slash(str(p))


def _slash(text: str) -> str:
    """Forward slashes in a *path*.

    The old rendering ran this over the whole ``path:line:text`` line, so a
    backslash in the matched source text was rewritten too. Now only the path
    column goes through it, and the text column comes back as it is on disk.
    """
    return text.replace("\\", "/")


# Which result modes are worth a TOON table.
#
# Only `matches` is. Its three columns were rendered `path:line:text`, which a
# Windows drive letter makes unsplittable -- `C:\src\a.py:12:x` splits to `C` --
# and TOON's quoting is what fixes that. It costs about 12% more tokens than the
# colon form, measured with o200k_base, and that is what the fix is worth.
#
# `files` and `counts` are not. A bare path list and `path:12` both split
# correctly already, so a table there buys nothing and costs 10-30% more tokens.
# They keep their lines and get a count marker, which is the other half of what
# the table was for: knowing whether the answer was cut.
_TABULAR = frozenset({"matches"})


def _line_for(key: str, row: dict[str, Any]) -> str:
    """A single result as one line, for the modes that do not need a table."""
    if key == "counts":
        # The count is digits after the *last* colon, so a path containing one
        # still comes apart correctly from the right.
        return f"{row['path']}:{row['matches']}"
    return str(row["path"])


def _emit(
    key: str,
    rows: list[dict[str, Any]],
    total: int,
    *,
    approx: bool = False,
    hint: str = HINT,
) -> str:
    """One block per result, capped by dropping rows rather than characters.

    Slicing the encoded string -- what ``_cap`` used to do -- would leave a
    header declaring more rows than follow it, and that count is the one thing
    the model is supposed to be able to check. Re-encoding a shorter row list
    costs a few passes and keeps the header honest.
    """
    tabular = key in _TABULAR

    def render(subset: list[dict[str, Any]]) -> str:
        if tabular:
            return toon.fenced({key: subset})
        head = f'<{key} count="{len(subset)}"/>'
        return "\n".join([head, *(_line_for(key, r) for r in subset)])

    shown = list(rows)
    body = render(shown)
    while len(body) > MAX_OUTPUT_CHARS and len(shown) > 1:
        keep = min(len(shown) - 1, max(1, len(shown) * MAX_OUTPUT_CHARS // len(body)))
        shown = shown[:keep]
        body = render(shown)
    if len(shown) < total:
        seen = f"{total}+" if approx else str(total)
        body += f'\n<truncated shown="{len(shown)}" total="{seen}" hint="{hint}"/>'
    return body
