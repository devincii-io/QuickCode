"""Scan the two plugin directories, resolve shadowing, apply the trust gate.

Layering, later shadowing earlier **by plugin id**, not by filename:

1. ``~/.quickcode/plugins/*.md``      user scope   -- every project
2. ``<cwd>/.quickcode/plugins/*.md``  project scope -- this repo, committed

A project beats the user, matching ``state.py``, ``preset.py``, ``mcp.py`` and
``definitions.py`` -- every existing layering in the codebase. Shadowing is
resolved *before* registration, because ``PluginRegistry.register`` keeps the
first spec for an id and discovery must therefore hand it one winner per id,
not two and a hope.

``.trash/`` is a subdirectory and ``glob("*.md")`` does not descend, which is
the whole reason the scan must never become recursive.

Nothing here raises. A directory that cannot be read, a file that cannot be
decoded and a document that fails validation all produce problems and no
plugin: one malformed file must not stop the app starting, and must not hide
the plugins that are fine.

**The trust gate.** ``.quickcode/plugins/`` is committed, so a project-scope
command tool is executable content in exactly the way an MCP server is:
cloning a repository would otherwise hand it the ability to define a tool the
agent may run. Project-scope ``kind: tool`` files are therefore inert until the
project is trusted, through the same gate ``quickcode/security/trust.py``
already applies to ``mcpServers``.

Authored **agents** and **prompt sections** are deliberately *not* gated, and
the line is capability rather than influence. An authored tool adds an
executable path that did not exist. An agent definition and a prompt section
add text -- and this app already quotes a repository's own ``QUICKCODE.md``
into the system prompt verbatim, untrusted, on every session, and has loaded
``.quickcode/agents/*.md`` untrusted since agents became authorable. Gating the
new text while the old text walks straight in would be theatre that also breaks
a documented feature. Neither can widen a capability: a definition's tool list
and ceiling are *intersected* by the resolver, never unioned. What they get
instead is visibility -- an ``info`` problem naming what the project
contributes, so a reader is told rather than surprised.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from quickcode.kernel.authoring import schema
from quickcode.kernel.authoring.format import parse_document
from quickcode.kernel.authoring.model import AuthoredPlugin
from quickcode.kernel.problems import Problem, Provenance

log = logging.getLogger("quickcode.kernel.authoring")

PLUGINS_DIRNAME = "plugins"
TRASH_DIRNAME = ".trash"


def user_plugins_dir() -> Path:
    """``~/.quickcode/plugins``. Read through the module so a test can move it."""
    from quickcode import config as config_module

    return Path(config_module.CONFIG_DIR) / PLUGINS_DIRNAME


def project_plugins_dir(cwd: Path | str) -> Path:
    return Path(cwd) / ".quickcode" / PLUGINS_DIRNAME


@dataclass
class Discovery:
    plugins: list[AuthoredPlugin] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)

    def by_kind(self, kind: str) -> list[AuthoredPlugin]:
        return [p for p in self.plugins if p.kind == kind]

    def get(self, plugin_id: str) -> AuthoredPlugin | None:
        for plugin in self.plugins:
            if plugin.id == plugin_id:
                return plugin
        return None


def discover(cwd: Path | str | None, *, trusted: bool | None = None) -> Discovery:
    """Every authored plugin this project loads, plus every reason one did not.

    ``trusted`` overrides the trust lookup; leave it ``None`` in production and
    pass it in tests so nothing reads the real ``~/.quickcode/trust.json``.
    """
    result = Discovery()
    winners: dict[str, AuthoredPlugin] = {}

    scopes: list[tuple[str, Path]] = []
    try:
        scopes.append(("user", user_plugins_dir()))
    except Exception:  # a home directory that cannot be resolved
        pass
    if cwd is not None:
        scopes.append(("project", project_plugins_dir(cwd)))

    for scope, directory in scopes:
        found, problems = _scan(directory, scope)
        result.problems.extend(problems)
        for plugin in found:
            winners[plugin.id] = plugin  # project scope is visited last

    project_plugins = [p for p in winners.values() if p.scope == "project"]
    if project_plugins:
        result.problems.extend(_trust_problems(cwd, project_plugins, trusted))
        if not _is_trusted(cwd, trusted):
            for plugin in project_plugins:
                if plugin.kind == "tool":
                    winners.pop(plugin.id, None)

    result.plugins = sorted(winners.values(), key=lambda p: (p.kind, p.name))
    return result


def _scan(directory: Path, scope: str) -> tuple[list[AuthoredPlugin], list[Problem]]:
    problems: list[Problem] = []
    accepted: dict[str, AuthoredPlugin] = {}
    claimed: dict[str, str] = {}  # id -> first path that claimed it

    try:
        if not directory.is_dir():
            return [], []
        # Never recursive: .trash/ lives underneath and must not be scanned.
        files = sorted(directory.glob("*.md"))
    except OSError as exc:
        log.warning("could not read %s: %s", directory, exc)
        return [], []

    for path in files:
        if path.name.startswith("."):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(Problem(
                code="bad_json", severity="error",
                message=f"{path.name} could not be read: {exc}",
                fix="Check the file's encoding; authored plugins are UTF-8.",
                subject=path.stem,
                provenance=Provenance(layer=_layer(scope), source=path.name,
                                      path=str(path)),
            ))
            continue

        doc = parse_document(text)
        plugin, found = schema.validate(
            doc, scope=scope, path=str(path), default_name=path.stem,
            source_text=text,
        )
        problems.extend(found)
        if plugin is None:
            continue
        prior = claimed.get(plugin.id)
        if prior is not None:
            # Two files in one directory claiming one id. Guessing which was
            # meant is worse than loading neither, so both go.
            accepted.pop(plugin.id, None)
            problems.append(Problem(
                code=schema.ID_DUPLICATE, severity="error",
                message=(f"'{plugin.id}' is claimed by two files in the same "
                         f"scope: {Path(prior).name} and {path.name}"),
                fix=("Give one of them a different 'name:'. Both are skipped "
                     "until they disagree about which is which."),
                subject=plugin.id,
                provenance=Provenance(layer=_layer(scope), source=path.name,
                                      path=str(path)),
            ))
            continue
        claimed[plugin.id] = str(path)
        accepted[plugin.id] = plugin

    return list(accepted.values()), problems


def _layer(scope: str) -> str:
    return "project" if scope == "project" else "user"


def _is_trusted(cwd: Path | str | None, trusted: bool | None) -> bool:
    if trusted is not None:
        return trusted
    if cwd is None:
        return False
    try:
        from quickcode.security import trust

        return trust.is_trusted(cwd)
    except Exception:  # a broken trust store means untrusted, never trusted
        return False


def _trust_problems(
    cwd: Path | str | None,
    project_plugins: list[AuthoredPlugin],
    trusted: bool | None,
) -> list[Problem]:
    tools = [p for p in project_plugins if p.kind == "tool"]
    text = [p for p in project_plugins if p.kind != "tool"]
    out: list[Problem] = []
    root = str(cwd) if cwd is not None else ""

    if tools and not _is_trusted(cwd, trusted):
        listed = ", ".join(sorted(p.name for p in tools))
        out.append(Problem(
            code=schema.NEEDS_TRUST, severity="error",
            message=(f"this project defines {len(tools)} command tool"
                     f"{'s' if len(tools) != 1 else ''} ({listed}) and is not "
                     "trusted, so they are inert"),
            fix=("Read the files, then trust this project to let its tools "
                 "run. An authored command tool executes a program, which is "
                 "the same thing a committed MCP server is."),
            subject="project", field="trust",
            provenance=Provenance(layer="project", source=".quickcode/plugins",
                                  path=root),
        ))
    if text:
        kinds = sorted({p.kind for p in text})
        out.append(Problem(
            code="authored_project_content", severity="info",
            message=(f"this project contributes {len(text)} authored "
                     f"{'/'.join(kinds)} definition"
                     f"{'s' if len(text) != 1 else ''} from .quickcode/plugins"),
            fix=("Nothing to do. They steer the model but cannot widen what it "
                 "may do -- tool lists and permission ceilings are intersected, "
                 "never extended. Read them if the repository is not yours."),
            subject="project", field="",
            provenance=Provenance(layer="project", source=".quickcode/plugins",
                                  path=root),
        ))
    return out


# --------------------------------------------------------------------------
# what the runtime asks for
# --------------------------------------------------------------------------

def command_tools(cwd: Path | str | None, *, trusted: bool | None = None) -> list:
    """The authored command tools this project runs. Never raises."""
    try:
        found = discover(cwd, trusted=trusted)
    except Exception as exc:  # discovery is best-effort by contract
        log.warning("authored plugin discovery failed: %s", exc)
        return []
    out = []
    for plugin in found.by_kind("tool"):
        try:
            out.append(plugin.to_tool())
        except Exception as exc:
            log.warning("could not build authored tool %s: %s", plugin.id, exc)
    return out


def tools_for_review(cwd: Path | str | None) -> list[dict]:
    """What this project's command tools would run, for the trust prompt.

    Deliberately ignores trust: an untrusted tool is dropped from the registry,
    so by the time the banner asks about it there is nothing left to read --
    and asking someone to approve a filename is the thing the prompt exists to
    prevent. Reading a file in order to show it is not running it.

    Never builds a tool and never raises; a file too broken to parse still gets
    a row, because "we could not read this one" is the most important row here.
    """
    try:
        found = discover(cwd, trusted=True)
    except Exception as exc:
        log.warning("could not read authored tools for review: %s", exc)
        return []
    out: list[dict] = []
    for plugin in found.by_kind("tool"):
        if plugin.scope != "project":
            continue  # user-scope tools are the user's own files, never gated
        out.append({
            "name": plugin.name,
            "file": Path(plugin.path).name if plugin.path else "",
            "path": plugin.path,
            "label": plugin.label or "",
            "argv": list(plugin.argv),
        })
    return sorted(out, key=lambda t: t["name"])


def prompt_sections(cwd: Path | str | None, *, trusted: bool | None = None) -> list:
    """Authored ``PromptSection``s for the main agent, ordered. Never raises."""
    try:
        found = discover(cwd, trusted=trusted)
    except Exception as exc:
        log.warning("authored plugin discovery failed: %s", exc)
        return []
    out = []
    for plugin in found.by_kind("prompt"):
        if "main" not in plugin.applies_to:
            continue
        try:
            out.append(plugin.to_prompt_section())
        except Exception as exc:
            log.warning("could not build authored section %s: %s", plugin.id, exc)
    return out


def agent_defs(cwd: Path | str | None, *, trusted: bool | None = None) -> dict:
    """Authored ``AgentDef``s keyed by name. Never raises."""
    try:
        found = discover(cwd, trusted=trusted)
    except Exception as exc:
        log.warning("authored plugin discovery failed: %s", exc)
        return {}
    out = {}
    for plugin in found.by_kind("agent"):
        try:
            defn = plugin.to_agent_def()
        except Exception as exc:
            log.warning("could not build authored agent %s: %s", plugin.id, exc)
            continue
        if defn is not None:
            out[defn.name] = defn
    return out


def relative_to_home(path: Path) -> str:
    """A path a person can read back: ``~/.quickcode/...`` where it applies."""
    try:
        return "~" + os.sep + str(path.relative_to(Path.home()))
    except (ValueError, OSError):
        return str(path)
