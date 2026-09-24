"""Adding, changing and removing hooks in the settings files the loader reads.

The files stay the truth. Everything here reads one settings file, changes the
``hooks`` block in place and writes the whole file back -- beside it first,
then renamed over it -- so a crash leaves the old file or the new one, and
every key this module does not own (permissions, mcpServers, plugins) comes
through untouched.

A hook is addressed by its plugin id (``HookCommand.id``) and the file it sits
in. The id is derived from the event, the matcher and the command, and entries
are matched by running them through ``config.parse`` -- the loader's own
parser -- so "this entry is that hook" is decided by the same code that
decides what runs.

**A project write keeps the project's trust, and grants none.** The trust hash
covers the ``hooks`` blocks, so a write to a project file goes through
``trust.keep_trust``: a trusted project stays trusted after you edit its hooks
here, and a project that was not trusted stays untrusted -- a hook added to it
is saved, listed as refused, and runs only once the project is trusted.

A settings file that does not parse is refused rather than rewritten: writing
over it would replace everything in it with the one block this module knows.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quickcode.hooks.config import (
    EVENTS,
    ID_PREFIX,
    MAX_TIMEOUT_S,
    TOOL_EVENTS,
    HookCommand,
    HookConfig,
    Scope,
    load_hooks,
    parse,
)
from quickcode.kernel import state as state_store
from quickcode.security import trust

# The project files, named as the loader names them.
PROJECT_FILES = tuple(rel.name for rel in trust.PROJECT_SETTINGS_FILES)
USER_FILE = state_store.SETTINGS_FILENAME

# One alternative of a matcher: the characters a tool name is made of (MCP
# servers name their tools, so dots, colons and dashes occur) plus glob syntax.
_ALTERNATIVE = re.compile(r"[\w.:*?\[\]!-]+")
_REGEX_HINTS = re.compile(r"\.\*|\.\+|[\\^$()+{}]")

_ID = re.compile(rf"{re.escape(ID_PREFIX)}(user|project)\.[a-z_]+\.[0-9a-f]{{10}}\Z")

# Read-modify-write on a settings file is not atomic across two requests.
_LOCK = threading.Lock()


class HookEditError(ValueError):
    def __init__(self, message: str, *, status: int = 400, fix: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.fix = fix


@dataclass(frozen=True)
class Draft:
    """A hook as the form sends it, validated."""

    event: str
    command: str
    matcher: str = ""
    # None: not stated, so the loader's default applies and nothing is written.
    timeout_s: float | None = None

    def as_hook(self, scope: Scope, source: str = "") -> HookCommand:
        kwargs: dict[str, Any] = {} if self.timeout_s is None else {"timeout_s": self.timeout_s}
        return HookCommand(event=self.event, command=self.command, scope=scope,
                           matcher=self.matcher, source=source, **kwargs)


@dataclass(frozen=True)
class Declared:
    """One hook as a settings file declares it, and what happens to it."""

    hook: HookCommand
    file: str
    # "active" | "disabled" (switched off in Settings) | "refused" (a project
    # hook in an untrusted project: saved, never run)
    status: str


# ---- validation -------------------------------------------------------------


def check_matcher(event: str, matcher: str) -> str:
    """The matcher, stripped, or a ``HookEditError`` saying what is wrong.

    Stricter than the loader, which ignores what it cannot use: a form is the
    place to say so, before a guard is saved that silently selects nothing.
    """
    matcher = matcher.strip()
    if matcher in ("", "*"):
        return matcher
    if event not in TOOL_EVENTS:
        raise HookEditError(
            f"{event} is not about a tool, so a matcher would be ignored",
            fix="Leave the matcher empty.")
    for raw in matcher.split("|"):
        alt = raw.strip()
        if not alt:
            raise HookEditError(
                f"matcher {matcher!r} has an empty alternative, which matches nothing",
                fix="Remove the stray '|'.")
        if _REGEX_HINTS.search(alt):
            raise HookEditError(
                f"{alt!r} reads like a regular expression; matchers are globs",
                fix=f"Write it as a glob, for example {alt.replace('.*', '*')!r}.")
        if not _ALTERNATIVE.fullmatch(alt):
            raise HookEditError(
                f"{alt!r} is not a tool name or a glob",
                fix="Use tool names and * ? [ ], separated by '|': bash, write|edit, mcp__*.")
        depth = 0
        for ch in alt:
            depth += ch == "["
            depth -= ch == "]"
            if depth not in (0, 1):
                break
        if depth:
            raise HookEditError(f"{alt!r} has an unbalanced '[' or ']'")
    return matcher


def check_timeout(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool) or not isinstance(raw, int | float) or not math.isfinite(raw):
        raise HookEditError("timeout must be a number of seconds")
    if raw <= 0 or raw > MAX_TIMEOUT_S:
        raise HookEditError(f"timeout must be more than 0 and at most {MAX_TIMEOUT_S:g} seconds")
    return float(raw)


def draft_from(body: dict[str, Any]) -> Draft:
    event = body.get("event")
    if not isinstance(event, str) or event not in EVENTS:
        near = next((e for e in EVENTS if e.lower() == str(event).lower()), "")
        raise HookEditError(
            f"{event!r} is not a hook event",
            fix=f"Did you mean {near}?" if near else f"Use one of {', '.join(EVENTS)}.")
    command = body.get("command")
    if not isinstance(command, str) or not command.strip():
        raise HookEditError("a hook needs a command")
    if "\0" in command:
        raise HookEditError("a command cannot contain a NUL character")
    matcher = body.get("matcher", "")
    if matcher is None:
        matcher = ""
    if not isinstance(matcher, str):
        raise HookEditError("matcher must be a string")
    return Draft(event=event, command=command.strip(), matcher=check_matcher(event, matcher),
                 timeout_s=check_timeout(body.get("timeout")))


def scope_of(hook_id: str) -> Scope:
    m = _ID.fullmatch(hook_id)
    if not m:
        raise HookEditError(f"{hook_id!r} is not a command hook id", status=404)
    return "user" if m.group(1) == "user" else "project"


# ---- the files --------------------------------------------------------------


def settings_path(cwd: Path, scope: str, file: str = "") -> Path:
    if scope == "user":
        if file not in ("", USER_FILE):
            raise HookEditError(f"your own hooks live in {USER_FILE}, not {file!r}")
        return state_store.user_settings_path()
    if scope == "project":
        name = file or PROJECT_FILES[0]
        if name not in PROJECT_FILES:
            raise HookEditError(
                f"a project hook lives in {' or '.join(PROJECT_FILES)}, not {name!r}")
        return Path(cwd) / state_store.SETTINGS_DIRNAME / name
    raise HookEditError(f"scope must be 'user' or 'project', not {scope!r}")


def _read_strict(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    fix = "Fix the file by hand first: saving here would overwrite everything in it."
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HookEditError(f"{path} could not be read as JSON ({exc}), so it was left alone",
                            status=409, fix=fix) from exc
    if not isinstance(raw, dict):
        raise HookEditError(f"{path} is not a JSON object, so it was left alone",
                            status=409, fix=fix)
    return raw


def _block(raw: dict[str, Any], path: Path) -> dict[str, Any]:
    block = raw.get(trust.HOOKS_KEY)
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise HookEditError(f"'hooks' in {path} is not an object, so it was left alone",
                            status=409, fix="Fix the file by hand first.")
    return block


def _atomic_write(path: Path, raw: dict[str, Any]) -> None:
    data = (json.dumps(raw, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _save(cwd: Path, scope: str, path: Path, raw: dict[str, Any]) -> None:
    if scope != "project":
        _atomic_write(path, raw)
        return
    from quickcode.workspace import ensure_project_dir

    # The .gitignore that keeps settings.local.json out of a commit comes with
    # the directory, so a first write creates the directory the usual way.
    ensure_project_dir(cwd)
    with trust.keep_trust(cwd):
        _atomic_write(path, raw)


# ---- reading ----------------------------------------------------------------


def _scope_files(cwd: Path, scope: Scope) -> list[tuple[str, Path, Any]]:
    """``(file name, path, raw hooks block)`` for each file of one scope."""
    if scope == "user":
        path = state_store.user_settings_path()
        return [(USER_FILE, path, state_store._read(path).get(trust.HOOKS_KEY))]
    return [(Path(rel).name, Path(cwd) / rel, block)
            for rel, block in trust.project_hooks(cwd).items()]


def declared(cwd: Path, *, trusted: bool | None = None) -> tuple[list[Declared], HookConfig]:
    """Every hook the settings files declare, per file, with its status.

    Trust is read once and handed to the loader, so the statuses here and the
    loader's answer cannot disagree about it.
    """
    if trusted is None:
        trusted = trust.resolve_trust(cwd)
    config = load_hooks(cwd, trusted=trusted)
    off = {h.id for h in config.disabled}
    rows: list[Declared] = []
    for scope in ("user", "project"):
        for name, path, block in _scope_files(cwd, scope):
            hooks, _ = parse(block, scope=scope, source=str(path))
            seen: set[str] = set()
            for hook in hooks:
                if hook.id in seen:
                    continue
                seen.add(hook.id)
                if scope == "project" and not trusted:
                    status = "refused"
                else:
                    status = "disabled" if hook.id in off else "active"
                rows.append(Declared(hook=hook, file=name, status=status))
    return rows, config


def find(cwd: Path, hook_id: str, file: str = "") -> Declared:
    """The declared hook with this id, in ``file`` when one is named."""
    scope_of(hook_id)
    rows, _ = declared(cwd)
    for row in rows:
        if row.hook.id == hook_id and (not file or row.file == file):
            return row
    where = f" in {file}" if file else ""
    raise HookEditError(f"there is no hook {hook_id}{where}", status=404,
                        fix="It may have been edited outside the app; reload the list.")


def _exists(cwd: Path, scope: Scope, hook_id: str) -> str:
    """The file of this scope that already declares the hook, or ""."""
    for name, path, block in _scope_files(cwd, scope):
        hooks, _ = parse(block, scope=scope, source=str(path))
        if any(h.id == hook_id for h in hooks):
            return name
    return ""


# ---- editing ----------------------------------------------------------------


def _group_matcher(group: dict[str, Any]) -> str:
    matcher = group.get("matcher", "")
    return matcher.strip() if isinstance(matcher, str) else "\0"


def _positions(block: dict[str, Any], hook_id: str, scope: Scope) -> list[tuple[str, int, int]]:
    """Where in the block the entries that make up this hook are."""
    out: list[tuple[str, int, int]] = []
    for event, groups in block.items():
        if event not in EVENTS or not isinstance(groups, list):
            continue
        for gi, group in enumerate(groups):
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                continue
            for hi, entry in enumerate(group["hooks"]):
                found, _ = parse({event: [{"matcher": group.get("matcher", ""),
                                           "hooks": [entry]}]}, scope=scope)
                if found and found[0].id == hook_id:
                    out.append((event, gi, hi))
    return out


def _remove(block: dict[str, Any], positions: list[tuple[str, int, int]]) -> None:
    for event, gi, hi in sorted(positions, reverse=True):
        del block[event][gi]["hooks"][hi]
    for event, gi in sorted({(e, g) for e, g, _ in positions}, reverse=True):
        groups = block[event]
        if not groups[gi]["hooks"]:
            del groups[gi]
        if not groups:
            del block[event]


def _entry(draft: Draft, base: dict[str, Any] | None = None) -> dict[str, Any]:
    # Keys this module does not know are carried over, for a hook written by
    # hand with fields a newer version -- or Claude Code -- reads.
    entry = {k: v for k, v in (base or {}).items() if k not in ("command", "timeout")}
    entry.setdefault("type", "command")
    entry["command"] = draft.command
    if draft.timeout_s is not None:
        timeout = draft.timeout_s
        entry["timeout"] = int(timeout) if timeout.is_integer() else timeout
    return entry


def _insert(block: dict[str, Any], draft: Draft, entry: dict[str, Any], path: Path) -> None:
    groups = block.setdefault(draft.event, [])
    if not isinstance(groups, list):
        raise HookEditError(f"'hooks.{draft.event}' in {path} is not a list, so it was left "
                            "alone", status=409, fix="Fix the file by hand first.")
    for group in groups:
        if (isinstance(group, dict) and isinstance(group.get("hooks"), list)
                and _group_matcher(group) == draft.matcher):
            group["hooks"].append(entry)
            return
    group: dict[str, Any] = {"matcher": draft.matcher} if draft.matcher else {}
    group["hooks"] = [entry]
    groups.append(group)


def _refuse_duplicate(cwd: Path, scope: Scope, hook: HookCommand) -> None:
    where = _exists(cwd, scope, hook.id)
    if where:
        raise HookEditError(
            f"the same {hook.event} hook is already in {where}; the same command for the "
            "same event and matcher runs once, so a second copy would do nothing",
            status=409)


def add(cwd: Path, draft: Draft, *, scope: str, file: str = "") -> HookCommand:
    path = settings_path(cwd, scope, file)
    hook = draft.as_hook(scope, str(path))  # type: ignore[arg-type]
    with _LOCK:
        _refuse_duplicate(cwd, hook.scope, hook)
        raw = _read_strict(path)
        block = _block(raw, path)
        _insert(block, draft, _entry(draft), path)
        raw[trust.HOOKS_KEY] = block
        _save(cwd, scope, path, raw)
    return hook


def update(cwd: Path, hook_id: str, draft: Draft, *, file: str = "") -> HookCommand:
    """Replace one hook with ``draft``, where it is.

    When the event and matcher stay the same the entry is changed in place;
    otherwise it moves to a group that fits. A hook written twice in the file
    is one hook to the loader, so every copy is replaced -- leaving one behind
    would leave the old command running.
    """
    scope = scope_of(hook_id)
    if not file:
        file = find(cwd, hook_id).file
    path = settings_path(cwd, scope, file)
    hook = draft.as_hook(scope, str(path))
    with _LOCK:
        if hook.id != hook_id:
            _refuse_duplicate(cwd, scope, hook)
        raw = _read_strict(path)
        block = _block(raw, path)
        positions = _positions(block, hook_id, scope)
        if not positions:
            raise HookEditError(f"there is no hook {hook_id} in {path}", status=404,
                                fix="It may have been edited outside the app; reload the list.")
        event, gi, hi = positions[0]
        group = block[event][gi]
        old = group["hooks"][hi]
        if event == draft.event and _group_matcher(group) == draft.matcher:
            group["hooks"][hi] = _entry(draft, old)
            _remove(block, positions[1:])
        else:
            _remove(block, positions)
            _insert(block, draft, _entry(draft, old), path)
        raw[trust.HOOKS_KEY] = block
        _save(cwd, scope, path, raw)
    return hook


def remove(cwd: Path, hook_id: str, *, file: str = "") -> int:
    """Delete every entry that makes up this hook from its file. Returns how many."""
    scope = scope_of(hook_id)
    if not file:
        file = find(cwd, hook_id).file
    path = settings_path(cwd, scope, file)
    with _LOCK:
        raw = _read_strict(path)
        block = _block(raw, path)
        positions = _positions(block, hook_id, scope)
        if not positions:
            raise HookEditError(f"there is no hook {hook_id} in {path}", status=404,
                                fix="It may have been edited outside the app; reload the list.")
        _remove(block, positions)
        if block:
            raw[trust.HOOKS_KEY] = block
        else:
            raw.pop(trust.HOOKS_KEY, None)
        _save(cwd, scope, path, raw)
    return len(positions)
