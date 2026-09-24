"""The one reader and writer of QuickCode's ``settings.json`` files.

A project's settings files are covered by the trust hash, so *any* write to one
moves the hash and untrusts the project. A write the app makes on the user's
behalf -- a plugin knob, a composition, a profile, an "Always allow" -- is not
a reason to stop trusting it, and every such write goes through
:func:`write_project_settings`, which re-binds a grant the project already had.
Before there was one writer, some saves did that and some did not.

A write reads the whole file, lets the caller change it in place and writes all
of it back, so no caller clobbers keys it does not own -- and refuses, with
:class:`SettingsUnreadable`, a file it cannot parse rather than replace it.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from quickcode import jsonfile
from quickcode.fsutil import atomic_write_text
from quickcode.security import trust

log = logging.getLogger("quickcode.kernel.settings_file")

SETTINGS_DIRNAME = ".quickcode"
SETTINGS_FILENAME = "settings.json"
LOCAL_SETTINGS_FILENAME = "settings.local.json"

T = TypeVar("T")


class SettingsUnreadable(ValueError):
    """A settings file exists but cannot be merged into, so it is not written."""

    def __init__(self, path: Path, problem: str) -> None:
        self.path = path
        super().__init__(
            f"{path} {problem}. Nothing was saved, because saving would replace "
            "everything in it; fix the file (or remove it) and try again."
        )


def read_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = jsonfile.load(path)
    except (OSError, ValueError) as exc:
        # A hand-edited settings file with a stray comma must not take the app
        # down; the layer is skipped and the user is told once, in the log.
        log.warning("ignoring unreadable settings at %s: %s", path, exc)
        return {}
    return raw if isinstance(raw, dict) else {}


def write_settings(path: Path, mutate: Callable[[dict[str, Any]], T]) -> T:
    """Read-modify-write a settings file outside any project: the user's own.

    A symlink is written through rather than replaced, the way the plain write
    this replaced behaved, so a settings file kept in a dotfiles repository
    stays linked.
    """
    return _write(Path(os.path.realpath(path)), mutate)


def write_project_settings(cwd: str | os.PathLike[str],
                           mutate: Callable[[dict[str, Any]], T], *,
                           filename: str = SETTINGS_FILENAME) -> T:
    """Read-modify-write one of a project's settings files, keeping its trust.

    A project that was trusted before the write is trusted for what it holds
    after; one that was not stays untrusted, so a save can never grant trust.
    Unlike :func:`write_settings`, a symlink is replaced rather than followed:
    the link is the repository's, and its target can be anywhere.
    """
    path = Path(cwd) / SETTINGS_DIRNAME / filename
    with trust.keep_trust(cwd):
        return _write(path, mutate)


def _write(path: Path, mutate: Callable[[dict[str, Any]], T]) -> T:
    raw = _read_for_write(path)
    result = mutate(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(raw, indent=2))
    return result


def _read_for_write(path: Path) -> dict[str, Any]:
    """The file as a dict to merge into, or a refusal.

    Stricter than :func:`read_settings` on purpose: a reader can skip a file
    it cannot parse, but a writer that did the same would write back only the
    key it owns and erase everything else the file held.
    """
    try:
        text = jsonfile.decode(path.read_bytes())
    except FileNotFoundError:
        return {}
    except UnicodeDecodeError as exc:
        raise SettingsUnreadable(path, "is not UTF-8 text") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SettingsUnreadable(path, f"is not valid JSON ({exc})") from exc
    if not isinstance(raw, dict):
        raise SettingsUnreadable(path, "does not hold a JSON object")
    return raw
