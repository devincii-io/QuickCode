"""Hook configuration: which commands run, on which event, for which tool.

The shape is the one Claude Code uses, so a hooks block can be carried across::

    {"hooks": {"PreToolUse": [{"matcher": "bash|mcp__*",
                               "hooks": [{"type": "command",
                                          "command": "python guard.py",
                                          "timeout": 10}]}]}}

Two layers, both of which run. ``~/.quickcode/settings.json`` is the user's own
and is never gated. The project's ``.quickcode/settings.json`` and
``settings.local.json`` are a repository asking to run a program, so their
hooks run only in a trusted project (``security/trust.py``); otherwise they are
listed as refused and nothing of them executes.

A hook's plugin id is derived from what it runs rather than where it sits in
the file, so reordering the block does not move a disabled hook's switch onto
a different command.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Literal

from quickcode.kernel import state as state_store
from quickcode.kernel.problems import (
    HOOK_INVALID,
    HOOK_MATCHER_IGNORED,
    HOOK_REFUSED,
    Problem,
    Provenance,
)
from quickcode.kernel.settings_file import read_settings
from quickcode.security import trust

EVENTS = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop", "SessionStart")
TOOL_EVENTS = frozenset({"PreToolUse", "PostToolUse"})

DEFAULT_TIMEOUT_S = 30.0
MAX_TIMEOUT_S = 600.0

ID_PREFIX = "hook.cmd."

Scope = Literal["user", "project"]


def _snake(event: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", event).lower()


def matcher_matches(matcher: str, tool_name: str) -> bool:
    """Whether a matcher selects this tool.

    Empty or ``*`` selects every tool. Otherwise ``|`` separates alternatives,
    each an exact name or a glob (``mcp__*``, ``task_*``). Case-insensitive:
    QuickCode's tools are lower-case and Claude Code's are not, and a guard
    written as ``Bash`` that silently matched nothing would fail open.
    """
    pattern = (matcher or "").strip()
    if pattern in ("", "*"):
        return True
    name = tool_name.lower()
    return any(
        fnmatchcase(name, alt.strip().lower()) for alt in pattern.split("|") if alt.strip()
    )


@dataclass(frozen=True)
class HookCommand:
    event: str
    command: str
    scope: Scope
    matcher: str = ""
    timeout_s: float = DEFAULT_TIMEOUT_S
    # The settings file this came from, for the Settings card and problems.
    source: str = ""

    @property
    def id(self) -> str:
        digest = hashlib.sha256(
            f"{self.event}\0{self.matcher}\0{self.command}".encode()
        ).hexdigest()[:10]
        return f"{ID_PREFIX}{self.scope}.{_snake(self.event)}.{digest}"

    def matches(self, tool_name: str) -> bool:
        return self.event not in TOOL_EVENTS or matcher_matches(self.matcher, tool_name)


@dataclass(frozen=True)
class HookConfig:
    """Everything the settings files declared, sorted by what will happen to it."""

    hooks: tuple[HookCommand, ...] = ()
    # Project hooks in an untrusted project: declared, never run.
    refused: tuple[HookCommand, ...] = ()
    # Switched off from Settings (``plugins.<id>.enabled: false``).
    disabled: tuple[HookCommand, ...] = ()
    problems: tuple[Problem, ...] = ()

    def matching(self, event: str, tool_name: str = "") -> list[HookCommand]:
        return [h for h in self.hooks if h.event == event and h.matches(tool_name)]


def _problem(code: str, severity: str, message: str, source: str, pointer: str,
             scope: Scope, fix: str = "") -> Problem:
    return Problem(
        code=code, severity=severity, message=message, fix=fix,  # type: ignore[arg-type]
        subject="hooks", field="hooks",
        provenance=Provenance(layer=scope, source=Path(source).name if source else "",
                              path=f"{source}#{pointer}"),
    )


def _timeout(raw: Any) -> float | None:
    if raw is None:
        return DEFAULT_TIMEOUT_S
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def parse(raw: Any, *, scope: Scope, source: str = "") -> tuple[list[HookCommand], list[Problem]]:
    """One ``hooks`` block into commands, and a problem for every entry dropped.

    An entry is dropped on its own: the entries are independent, and losing a
    whole block to one typo would switch off guards that were written fine.
    """
    hooks: list[HookCommand] = []
    problems: list[Problem] = []
    if raw is None:
        return hooks, problems

    def bad(pointer: str, message: str, fix: str = "") -> None:
        problems.append(_problem(HOOK_INVALID, "error", message, source, pointer, scope, fix))

    if not isinstance(raw, dict):
        bad("/hooks", "hooks must be an object keyed by event name",
            f"Use one of {', '.join(EVENTS)} as keys.")
        return hooks, problems

    for event, groups in raw.items():
        where = f"/hooks/{event}"
        if event not in EVENTS:
            near = next((e for e in EVENTS if e.lower() == str(event).lower()), "")
            bad(where, f"'{event}' is not a hook event, so nothing under it runs",
                f"Did you mean {near}?" if near else f"Use one of {', '.join(EVENTS)}.")
            continue
        if not isinstance(groups, list):
            bad(where, f"{event} must be a list of matcher groups")
            continue
        for gi, group in enumerate(groups):
            gwhere = f"{where}/{gi}"
            if not isinstance(group, dict):
                bad(gwhere, "a matcher group must be an object with a 'hooks' list")
                continue
            matcher = group.get("matcher", "")
            if not isinstance(matcher, str):
                bad(f"{gwhere}/matcher", "matcher must be a string")
                continue
            if matcher.strip() not in ("", "*") and event not in TOOL_EVENTS:
                problems.append(_problem(
                    HOOK_MATCHER_IGNORED, "info",
                    f"{event} is not about a tool, so its matcher '{matcher}' is ignored "
                    "and the hook runs every time", source, f"{gwhere}/matcher", scope,
                    "Remove the matcher."))
                matcher = ""
            entries = group.get("hooks")
            if not isinstance(entries, list):
                bad(f"{gwhere}/hooks", "a matcher group needs a 'hooks' list")
                continue
            for hi, entry in enumerate(entries):
                hwhere = f"{gwhere}/hooks/{hi}"
                if not isinstance(entry, dict):
                    bad(hwhere, "a hook must be an object with a 'command'")
                    continue
                if entry.get("type", "command") != "command":
                    bad(f"{hwhere}/type", f"hook type '{entry.get('type')}' is not supported",
                        "Only command hooks exist: set type to \"command\".")
                    continue
                command = entry.get("command")
                if not isinstance(command, str) or not command.strip():
                    bad(f"{hwhere}/command", "a hook needs a non-empty command string")
                    continue
                timeout = _timeout(entry.get("timeout"))
                if timeout is None:
                    bad(f"{hwhere}/timeout", "timeout must be a positive number of seconds")
                    continue
                if timeout > MAX_TIMEOUT_S:
                    problems.append(_problem(
                        HOOK_INVALID, "warning",
                        f"timeout {timeout:g}s is above the {MAX_TIMEOUT_S:g}s ceiling and "
                        "was lowered to it", source, f"{hwhere}/timeout", scope))
                    timeout = MAX_TIMEOUT_S
                hooks.append(HookCommand(event=event, command=command.strip(), scope=scope,
                                         matcher=matcher.strip(), timeout_s=timeout,
                                         source=source))
    return hooks, problems


def _dedupe(hooks: list[HookCommand]) -> list[HookCommand]:
    """The same command for the same event and matcher runs once, not twice.

    Identical entries also share an id, and one id must name one plugin.
    """
    seen: set[str] = set()
    out: list[HookCommand] = []
    for hook in hooks:
        if hook.id not in seen:
            seen.add(hook.id)
            out.append(hook)
    return out


def load_hooks(cwd: Path | None, *, trusted: bool | None = None) -> HookConfig:
    """User hooks, then the project's (trust permitting), minus switched-off ones.

    ``trusted`` is the test override ``trust.resolve_trust`` takes; leave it
    ``None`` in production.
    """
    user_path = state_store.user_settings_path()
    user_raw = read_settings(user_path).get(trust.HOOKS_KEY)
    user, problems = parse(user_raw, scope="user", source=str(user_path))

    project: list[HookCommand] = []
    if cwd is not None:
        for rel, raw in trust.project_hooks(cwd).items():
            found, more = parse(raw, scope="project", source=str(Path(cwd) / rel))
            project += found
            problems += more
    user, project = _dedupe(user), _dedupe(project)

    refused: list[HookCommand] = []
    if project and not trust.resolve_trust(cwd, trusted):
        refused, project = project, []
        events = sorted({h.event for h in refused}, key=EVENTS.index)
        problems.append(Problem(
            code=HOOK_REFUSED, severity="warning",
            message=(f"this project declares {len(refused)} "
                     f"{'hook' if len(refused) == 1 else 'hooks'} ({', '.join(events)}) "
                     "and is not trusted, so none of them run"),
            fix=("Read the commands, then trust this project to let them run. A "
                 "hook is a program the repository asks QuickCode to start for it."),
            subject="project", field="trust",
            provenance=Provenance(layer="project", source=state_store.SETTINGS_FILENAME,
                                  path=str(state_store.project_settings_path(cwd))),
        ))

    state = state_store.load_state(cwd, trusted=trusted)
    off = {pid for pid, entry in state.items() if entry.get("enabled") is False}
    active = [h for h in (*user, *project) if h.id not in off]
    disabled = [h for h in (*user, *project) if h.id in off]
    return HookConfig(hooks=tuple(active), refused=tuple(refused),
                      disabled=tuple(disabled), problems=tuple(problems))


def review_rows(blocks: dict[str, Any]) -> list[dict[str, str]]:
    """``trust.project_hooks`` output as rows for the trust review."""
    rows: list[dict[str, str]] = []
    for rel, raw in blocks.items():
        found, _ = parse(raw, scope="project", source=rel)
        rows += [{"event": h.event, "matcher": h.matcher, "command": h.command,
                  "file": rel} for h in found]
    return rows
