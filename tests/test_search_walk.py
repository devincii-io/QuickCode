"""Which files glob and grep look at, and in what order.

grep has two backends, ripgrep and a pure-Python walk, and the module that
owns them promises they agree. They did not: ripgrep honoured ``.gitignore``
and skipped hidden files, the walk did the opposite, and the walk also
excluded any file with ``venv`` or ``node_modules`` *anywhere* in its absolute
path -- so a project living under such a directory could not be searched at
all. glob used ``Path.glob`` and so ignored ``.gitignore`` too, although the
tool surface documents that it respects it.

The grep tests here run once per backend against real files, with the real
``rg`` when it is installed, so "they agree" is checked rather than assumed.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from quickcode.tools import grep as grep_module
from quickcode.tools.base import ReadRegistry, ToolCtx
from quickcode.tools.fs import patterns
from quickcode.tools.fs.walk import walk_files
from quickcode.tools.glob import GlobTool
from quickcode.tools.grep import GrepTool

HAVE_RG = shutil.which("rg") is not None
BACKENDS = [
    pytest.param("ripgrep", marks=pytest.mark.skipif(not HAVE_RG, reason="needs ripgrep")),
    "fallback",
]
CAN_SYMLINK = hasattr(os, "symlink") and os.name != "nt"


def put(root: Path, rel: str, text: str = "needle\n") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def rels(root: Path, lines: list[str]) -> list[str]:
    prefix = str(root).replace("\\", "/") + "/"
    return [line.removeprefix(prefix) for line in lines]


@pytest.fixture
def backend(request, monkeypatch):
    if request.param == "fallback":
        monkeypatch.setattr(grep_module.shutil, "which", lambda _name: None)
    return request.param


async def grep_files(root: Path, cwd: Path | None = None, **kw) -> list[str]:
    ctx = ToolCtx(cwd=cwd or root, read_registry=ReadRegistry())
    kw.setdefault("pattern", "needle")
    result = await GrepTool().run(GrepTool.Input(**kw), ctx)
    assert not result.is_error, result.content
    if result.content == "No matches found.":
        return []
    return rels(root, result.content.splitlines()[1:])


async def glob(root: Path, pattern: str, path: str | None = None) -> list[str]:
    ctx = ToolCtx(cwd=root, read_registry=ReadRegistry())
    result = await GlobTool().run(GlobTool.Input(pattern=pattern, path=path), ctx)
    assert not result.is_error, result.content
    if result.content == "No files matched.":
        return []
    return rels(root, result.content.splitlines()[1:])


def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


# ---- patterns ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "path", "hit"),
    [
        ("src/**/*.py", "src/a.py", True),
        ("src/**/*.py", "src/x/y/a.py", True),
        ("src/**/*.py", "lib/a.py", False),
        ("src/**/*.py", "src/a.pyc", False),
        ("*.py", "a.py", True),
        ("*.py", "deep/a.py", False),
        ("*.{ts,tsx}", "a.tsx", True),
        ("*.{ts,tsx}", "a.js", False),
        ("{src,lib}/**/x.*", "lib/q/x.c", True),
        ("[!a]*.py", "b.py", True),
        ("[!a]*.py", "a.py", False),
        ("a?c", "abc", True),
        ("a?c", "a/c", False),
        ("**", "any/thing/at/all", True),
        ("{weird}.txt", "{weird}.txt", True),
    ],
)
def test_glob_patterns(pattern, path, hit):
    assert bool(patterns.compile_glob(pattern).match(path)) is hit


def test_a_slashless_include_glob_matches_at_any_depth_like_ripgreps():
    assert patterns.compile_glob("*.py", anywhere=True).match("deep/down/a.py")
    assert not patterns.compile_glob("src/*.py", anywhere=True).match("x/src/a.py")


def test_a_brace_bomb_is_refused():
    with pytest.raises(patterns.PatternError):
        patterns.compile_glob("{a,b}" * 12)


def test_gitignore_rules_follow_gitignore5():
    rules = patterns.parse_ignore(
        "# comment\n\nbuild/\n*.log\n!keep.log\n/top.txt\ndocs/**/*.tmp\n\\#literal\n"
    )

    def ignored(path: str, is_dir: bool = False) -> bool:
        verdict = False
        for rule in rules:
            if rule.dir_only and not is_dir:
                continue
            if rule.regex.match(path):
                verdict = not rule.negate
        return verdict

    assert ignored("build", is_dir=True)
    assert ignored("x/build", is_dir=True)
    assert not ignored("build")               # a *file* called build
    assert ignored("x/y/debug.log")
    assert not ignored("keep.log")            # negated
    assert ignored("top.txt")
    assert not ignored("sub/top.txt")         # anchored by its leading slash
    assert ignored("docs/a/b/c.tmp")
    assert not ignored("other/a.tmp")
    assert ignored("#literal")


# ---- the walk ------------------------------------------------------------


def test_the_walk_is_in_component_order(tmp_path):
    for rel in ("z.py", "sub/b.py", "a.py", "sub.py", "m/n/o.py"):
        put(tmp_path, rel)

    assert [rel for rel, _ in walk_files(tmp_path)] == [
        "a.py", "m/n/o.py", "sub/b.py", "sub.py", "z.py",
    ]


def test_ignore_files_apply_inside_a_repo_only_and_nest(tmp_path):
    put(tmp_path, ".gitignore", "*.log\nbuild/\n")
    put(tmp_path, "keep.py")
    put(tmp_path, "debug.log")
    put(tmp_path, "build/out.py")
    put(tmp_path, "sub/.gitignore", "!special.log\n")
    put(tmp_path, "sub/special.log")
    put(tmp_path, "sub/other.log")

    outside = {rel for rel, _ in walk_files(tmp_path)}
    assert "debug.log" in outside           # no repository: .gitignore is not in force

    repo(tmp_path)
    inside = {rel for rel, _ in walk_files(tmp_path)}
    assert "keep.py" in inside
    assert "debug.log" not in inside
    assert "build/out.py" not in inside
    assert "sub/special.log" in inside      # re-included by the deeper file
    assert "sub/other.log" not in inside


def test_ignore_files_above_the_start_still_apply(tmp_path):
    repo(tmp_path)
    put(tmp_path, ".gitignore", "*.gen.py\n")
    put(tmp_path, ".git/info/exclude", "scratch/\n")
    put(tmp_path, "pkg/a.py")
    put(tmp_path, "pkg/a.gen.py")
    put(tmp_path, "pkg/scratch/x.py")

    assert [rel for rel, _ in walk_files(tmp_path / "pkg")] == ["a.py"]


def test_a_plain_ignore_file_works_without_a_repo(tmp_path):
    put(tmp_path, ".ignore", "vendor/\n")
    put(tmp_path, "vendor/lib.py")
    put(tmp_path, "main.py")

    assert [rel for rel, _ in walk_files(tmp_path)] == [".ignore", "main.py"]


@pytest.mark.skipif(not CAN_SYMLINK, reason="needs unprivileged symlinks")
def test_a_symlink_cycle_is_not_followed(tmp_path):
    put(tmp_path, "loop/a.py")
    os.symlink(tmp_path / "loop", tmp_path / "loop" / "again")
    os.symlink(tmp_path, tmp_path / "loop" / "up")

    assert [rel for rel, _ in walk_files(tmp_path)] == ["loop/a.py"]


def test_a_directory_reached_twice_is_walked_once(tmp_path, monkeypatch):
    """A bind mount (or a junction on a filesystem that does not report it)
    leads back into a directory already on the path. No link is involved, so
    only the directory's identity can stop the loop."""
    from quickcode.tools.fs import walk as walk_module

    put(tmp_path, "a/x.py")
    put(tmp_path, "a/mount/y.py")
    real_stat = os.stat
    a_identity = real_stat(tmp_path / "a")

    def stat(path, *args, **kwargs):
        if Path(path) == tmp_path / "a" / "mount":
            return a_identity
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(walk_module.os, "stat", stat)

    assert [rel for rel, _ in walk_files(tmp_path)] == ["a/x.py"]


def test_the_depth_bound_stops_the_descent(tmp_path):
    put(tmp_path, "a.py")
    put(tmp_path, "d/b.py")
    put(tmp_path, "d/e/c.py")

    assert [rel for rel, _ in walk_files(tmp_path, max_depth=2)] == ["a.py", "d/b.py"]


# ---- glob ----------------------------------------------------------------


async def test_glob_honours_gitignore_and_prunes_the_heavy_directories(tmp_path):
    repo(tmp_path)
    put(tmp_path, ".gitignore", "dist/\n")
    put(tmp_path, "src/a.js")
    put(tmp_path, "dist/bundle.js")
    put(tmp_path, "node_modules/pkg/index.js")

    assert await glob(tmp_path, "**/*.js") == ["src/a.js"]


async def test_glob_can_still_be_pointed_into_an_ignored_directory(tmp_path):
    repo(tmp_path)
    put(tmp_path, ".gitignore", "dist/\n")
    put(tmp_path, "dist/bundle.js")

    assert await glob(tmp_path, "dist/*.js") == ["dist/bundle.js"]


async def test_glob_lists_hidden_files(tmp_path):
    put(tmp_path, ".github/workflows/ci.yml")

    assert await glob(tmp_path, "**/*.yml") == [".github/workflows/ci.yml"]


async def test_glob_expands_braces(tmp_path):
    for rel in ("a.ts", "b.tsx", "c.js"):
        put(tmp_path, rel)

    assert sorted(await glob(tmp_path, "*.{ts,tsx}")) == ["a.ts", "b.tsx"]


async def test_glob_takes_an_absolute_pattern(tmp_path):
    put(tmp_path, "src/a.py")

    found = await glob(tmp_path, str(tmp_path / "src" / "*.py"))

    assert found == ["src/a.py"]


async def test_a_project_under_a_directory_named_venv_is_still_searchable(tmp_path):
    """Exclusions were tested against every *absolute* path component."""
    project = tmp_path / "venv" / "project"
    put(project, "src/a.py")

    assert await glob(project, "**/*.py") == ["src/a.py"]
    assert await grep_files(project) == ["src/a.py"]


async def test_glob_breaks_mtime_ties_by_path(tmp_path):
    for rel in ("c.py", "a.py", "b.py"):
        os.utime(put(tmp_path, rel), (1_000_000, 1_000_000))

    assert await glob(tmp_path, "*.py") == ["a.py", "b.py", "c.py"]


async def test_dot_dot_after_a_wildcard_is_an_error_not_a_walk(tmp_path):
    ctx = ToolCtx(cwd=tmp_path, read_registry=ReadRegistry())
    result = await GlobTool().run(GlobTool.Input(pattern="*/../../x"), ctx)

    assert result.is_error


# ---- grep: both backends see the same files --------------------------------


@pytest.mark.parametrize("backend", BACKENDS, indirect=True)
async def test_grep_honours_gitignore_on_both_backends(tmp_path, backend):
    repo(tmp_path)
    put(tmp_path, ".gitignore", "dist/\n*.log\n")
    put(tmp_path, "src/a.py")
    put(tmp_path, "dist/b.py")
    put(tmp_path, "debug.log")

    assert await grep_files(tmp_path) == ["src/a.py"]


@pytest.mark.parametrize("backend", BACKENDS, indirect=True)
async def test_grep_searches_hidden_files_inside_the_project(tmp_path, backend):
    put(tmp_path, ".github/workflows/ci.yml")
    put(tmp_path, "src/a.py")

    assert await grep_files(tmp_path) == [".github/workflows/ci.yml", "src/a.py"]


@pytest.mark.parametrize("backend", BACKENDS, indirect=True)
async def test_grep_skips_hidden_files_outside_the_project(tmp_path, backend):
    """Outside the project, dot-directories are where credentials live."""
    home = tmp_path / "home"
    put(home, ".config/gh/hosts.yml")
    put(home, "notes.txt")
    project = tmp_path / "project"
    project.mkdir()

    assert await grep_files(home, cwd=project, path=str(home)) == ["notes.txt"]


@pytest.mark.parametrize("backend", BACKENDS, indirect=True)
async def test_grep_never_walks_into_protected_or_heavy_directories(tmp_path, backend):
    for rel in (".env", ".env.local", ".ssh/id_rsa", ".quickcode/settings.json",
                ".git/config", "node_modules/pkg/i.js", "__pycache__/m.pyc", "ok.py"):
        put(tmp_path, rel)

    assert await grep_files(tmp_path) == ["ok.py"]


@pytest.mark.parametrize("backend", BACKENDS, indirect=True)
async def test_grep_can_be_aimed_into_an_excluded_directory(tmp_path, backend):
    put(tmp_path, "node_modules/pkg/index.js")

    found = await grep_files(tmp_path, path=str(tmp_path / "node_modules" / "pkg"))

    assert found == ["node_modules/pkg/index.js"]


@pytest.mark.parametrize("backend", BACKENDS, indirect=True)
async def test_grep_glob_filter_agrees_across_backends(tmp_path, backend):
    for rel in ("a.ts", "deep/b.tsx", "c.js", "src/d.ts"):
        put(tmp_path, rel)

    assert await grep_files(tmp_path, glob="*.{ts,tsx}") == ["a.ts", "deep/b.tsx", "src/d.ts"]
    assert await grep_files(tmp_path, glob="src/*.ts") == ["src/d.ts"]


@pytest.mark.parametrize("backend", BACKENDS, indirect=True)
async def test_overlapping_context_prints_each_line_once(tmp_path, backend):
    put(tmp_path, "a.txt", "0\nneedle 1\nneedle 2\n3\n4\n")
    ctx = ToolCtx(cwd=tmp_path, read_registry=ReadRegistry())

    result = await GrepTool().run(
        GrepTool.Input(pattern="needle", output_mode="content", context=1), ctx
    )

    lines = [row.strip().split(",")[1] for row in result.content.splitlines() if row.startswith("  ")]
    assert lines == ["1", "2", "3", "4"]


@pytest.mark.parametrize("backend", BACKENDS, indirect=True)
async def test_a_huge_matched_line_is_cut_to_a_window_around_the_match(tmp_path, backend):
    put(tmp_path, "bundle.min.js", "x" * 200_000 + "needle" + "y" * 200_000 + "\n")
    ctx = ToolCtx(cwd=tmp_path, read_registry=ReadRegistry())

    result = await GrepTool().run(GrepTool.Input(pattern="needle", output_mode="content"), ctx)

    assert len(result.content) < 2_000
    assert "needle" in result.content


@pytest.mark.parametrize("backend", BACKENDS, indirect=True)
async def test_line_numbers_agree_with_the_file_on_both_backends(tmp_path, backend):
    """A form feed is not a line break -- to ripgrep, to editors, and now to
    the fallback, which used ``str.splitlines``."""
    put(tmp_path, "a.py", "a\x0cb\nneedle\n")
    ctx = ToolCtx(cwd=tmp_path, read_registry=ReadRegistry())

    result = await GrepTool().run(GrepTool.Input(pattern="needle", output_mode="content"), ctx)

    row = next(r.strip() for r in result.content.splitlines() if r.startswith("  "))
    assert row.split(",")[1] == "2"
