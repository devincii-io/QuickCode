"""Protected paths are protected by the name they are written with, too.

The check used to run on the resolved path only, so a name was protected only
if it survived symlink resolution. On POSIX that meant a link inside
``.quickcode/artifacts/`` pointing anywhere else in the project read as that
other file -- and the read-only artifact exception then waved it through
(``test_permission_artifacts.py``'s symlink test, which failed on every Linux
run). It also meant every spelling Windows treats as the same name was a
different, unprotected one: ``.GIT``, ``.git.``, ``.env::$DATA``, ``GIT~1``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quickcode.core.permissions import Decision, Mode, PermissionEngine, Rules
from quickcode.security.protected import is_protected


def engine(mode: Mode = Mode.ask, root: Path | None = None, **rules) -> PermissionEngine:
    return PermissionEngine(mode=mode, rules=Rules(**rules), root=root or Path.cwd())


def _link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this machine")


def test_a_symlink_named_dotenv_is_protected_by_its_name(tmp_path):
    """`.env` -> `config/prod.cfg`: the target is an ordinary file, the name is
    the one the protection is about, and the read goes through that name."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "prod.cfg").write_text("API_KEY=1", encoding="utf-8")
    _link(tmp_path / ".env", tmp_path / "config" / "prod.cfg")
    e = engine(root=tmp_path, allow=["read"])
    assert e.evaluate("read", ".env") == Decision.ask
    assert e.evaluate("bash", "cat .env") == Decision.ask
    assert engine(Mode.dontask, root=tmp_path).evaluate("read", ".env") == Decision.deny


def test_a_symlink_to_a_protected_directory_is_still_protected(tmp_path):
    """The resolved half of the check, unchanged: a harmless name that lands in
    `.git` is `.git`."""
    (tmp_path / ".git").mkdir()
    _link(tmp_path / "gitlink", tmp_path / ".git")
    assert engine(root=tmp_path, allow=["edit"]).evaluate("edit", "gitlink/config") == Decision.ask


@pytest.mark.parametrize("path", [
    ".GIT/config", ".Git/hooks/pre-commit", ".ENV", ".Env.Local", ".SSH/id_rsa",
    ".QuickCode/settings.json",
])
def test_protected_names_are_compared_without_case(path, tmp_path):
    """NTFS and APFS open `.git` for `.GIT`."""
    assert engine(root=tmp_path, allow=["read", "write"]).evaluate("write", path) == Decision.ask


@pytest.mark.parametrize("path", [".git./config", ".git /config", ".env.", ".env ", ".ssh../x"])
def test_trailing_dots_and_spaces_do_not_hide_a_protected_name(path, tmp_path):
    """Win32 strips trailing dots and spaces from every component."""
    assert engine(root=tmp_path, allow=["write"]).evaluate("write", path) == Decision.ask


@pytest.mark.parametrize("path", [
    ".env::$DATA", ".env:secret", ".git::$INDEX_ALLOCATION/config", ".ssh:x/id_rsa",
])
def test_an_alternate_data_stream_names_its_file(path, tmp_path):
    """`.env::$DATA` is the unnamed stream of `.env` itself."""
    assert engine(root=tmp_path, allow=["read"]).evaluate("read", path) == Decision.ask


@pytest.mark.parametrize("path", ["GIT~1/config", "QUICKC~1/settings.json", "SSH~1/id_rsa",
                                  "ENV~1", "ENV~1.LOC", "GI4F2A~1/config"])
def test_an_8dot3_short_name_of_a_protected_name_is_protected(path, tmp_path):
    """Windows opens `.git` for `GIT~1` wherever short names are generated."""
    assert engine(root=tmp_path, allow=["read"]).evaluate("read", path) == Decision.ask


def test_ordinary_names_with_a_tilde_are_not_short_names(tmp_path):
    e = engine(root=tmp_path)
    assert e.evaluate("read", "HEAD~1") == Decision.allow
    assert e.evaluate("read", "notes~2.txt") == Decision.allow


def test_an_upcased_lookalike_is_the_same_name_on_ntfs(tmp_path):
    """NTFS upcases before comparing, and the dotless i upcases to `I`."""
    assert engine(root=tmp_path, allow=["read"]).evaluate("read", ".gıt/config") == (
        Decision.ask
    )


@pytest.mark.parametrize("path", ["src\\..\\.env", "src\\.git\\config", "a/b\\.ssh\\id_rsa"])
def test_a_backslash_is_a_separator_when_looking_for_protected_names(path, tmp_path):
    assert engine(root=tmp_path, allow=["read"]).evaluate("read", path) == Decision.ask


def test_a_project_under_a_protected_name_is_not_protected_wholesale(tmp_path):
    """The written check looks below the root: a project the user keeps under a
    directory called `.quickcode` is still a project, not one big secret."""
    root = tmp_path / ".quickcode" / "scratch"
    (root / "src").mkdir(parents=True)
    assert not is_protected(str(root / "src" / "a.py"), root)
    assert not is_protected("src/a.py", root)
    assert is_protected(str(root / ".env"), root)
