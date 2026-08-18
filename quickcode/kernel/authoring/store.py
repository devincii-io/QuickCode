"""Create, save, delete and duplicate authored plugin files.

Everything here is a file operation with a validator attached. The file is the
truth; these are the affordances that keep a person from having to remember
where the directory is and what the frontmatter keys are called.

**Save is advisory.** ``save_source`` writes what it was given and returns the
problems. Refusing to persist half-finished work would be hostile in an editor
and pointless besides -- the same file can be edited in vim.

**Delete is a move**, to ``.quickcode/plugins/.trash/<name>-<unix_ts>.md``. The
trash directory is not scanned (the scan is not recursive, deliberately). Undo
is a file move, and there is a strong need not to silently destroy a prompt
somebody spent an hour on.

**Duplicate materialises**, it does not inherit. A copy carries
``derived_from: <original id>`` as a breadcrumb and nothing else links the two.
Live inheritance would recreate exactly the coupling that makes the locked tier
necessary, and would mean an upgrade to QuickCode could change the behaviour of
a file you thought you owned. Duplicating *reads*, and reading was never
restricted, so it is offered at every tier including ``locked`` and
``required``.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from quickcode.kernel.authoring import schema
from quickcode.kernel.authoring.discovery import (
    TRASH_DIRNAME,
    discover,
    project_plugins_dir,
    user_plugins_dir,
)
from quickcode.kernel.authoring.format import parse_document
from quickcode.kernel.authoring.model import AuthoredPlugin
from quickcode.kernel.authoring.reserved import reserved_reason
from quickcode.kernel.authoring.templates import template
from quickcode.kernel.problems import Problem

SCOPES = ("user", "project")


class AuthoringError(Exception):
    """A refusal the UI renders as a message. Carries a ``fix`` and a status."""

    def __init__(self, message: str, *, fix: str = "", status: int = 400,
                 code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.fix = fix
        self.status = status
        self.code = code

    def to_json(self) -> dict[str, str]:
        return {"error": self.message, "fix": self.fix, "code": self.code}


def scope_dir(cwd: Path | str | None, scope: str) -> Path:
    if scope == "user":
        return user_plugins_dir()
    if cwd is None:
        raise AuthoringError("no project is open, so there is no project scope",
                             fix="Open a project, or use scope 'user'.")
    return project_plugins_dir(cwd)


def _slugify(raw: str) -> str:
    text = re.sub(r"[^a-z0-9_-]+", "-", (raw or "").strip().lower()).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    if text and not text[0].isalpha():
        text = f"p-{text}"
    return text[:32]


def _taken(directory: Path) -> set[str]:
    try:
        return {p.stem for p in directory.glob("*.md")}
    except OSError:
        return set()


def allocate_name(directory: Path, base: str, *, kind: str = "", copy: bool = False) -> str:
    """``base``, then ``base-copy``, ``base-copy-2``… until one is free.

    "Free" means both untaken *and* unreserved. A duplicate of
    ``agent.explore`` must not be called ``explore``: the id it would claim is
    the built-in one, which is refused rather than shadowed, and landing the
    user in an editor holding a file that cannot load is the worst possible
    outcome for a button whose whole promise is "now it is yours".
    """
    taken = _taken(directory)

    def free(candidate: str) -> bool:
        if candidate in taken:
            return False
        return not (kind and reserved_reason(f"{kind}.{candidate}", kind, candidate))

    if not copy and free(base):
        return base
    candidate = f"{base}-copy"[:32]
    if free(candidate):
        return candidate
    n = 2
    while True:
        candidate = f"{base}-copy-{n}"[:32]
        if free(candidate):
            return candidate
        n += 1


# --------------------------------------------------------------------------
# create / read / save / delete
# --------------------------------------------------------------------------

def create(
    cwd: Path | str | None,
    *,
    kind: str,
    name: str,
    scope: str = "project",
    title: str = "",
    text: str | None = None,
) -> tuple[Path, AuthoredPlugin | None, list[Problem]]:
    """Write a new plugin file, from the commented template unless ``text``."""
    if kind not in schema.KINDS:
        raise AuthoringError(
            f"'{kind}' is not an authorable kind",
            fix=f"Use one of: {', '.join(schema.KINDS)}.", code=schema.BAD_KIND)
    if scope not in SCOPES:
        raise AuthoringError(f"'{scope}' is not a scope",
                             fix="Use 'user' or 'project'.")
    slug = _slugify(name)
    if not slug:
        raise AuthoringError("that name has no usable characters in it",
                             fix="Use letters, digits, '-' and '_'.",
                             code=schema.BAD_SLUG)
    reason = reserved_reason(f"{kind}.{slug}", kind, slug)
    if reason:
        raise AuthoringError(
            f"'{kind}.{slug}' cannot be used: {reason}",
            fix="Pick a different name, or use Duplicate to start from the "
                "built-in one.",
            status=400, code=schema.ID_RESERVED)

    directory = scope_dir(cwd, scope)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{slug}.md"
    if path.exists():
        raise AuthoringError(
            f"{path.name} already exists in {scope} scope",
            fix="Pick another name, or edit the existing file.",
            status=409, code=schema.ID_DUPLICATE)

    body = text if text is not None else template(kind, slug, title)
    path.write_text(body, encoding="utf-8")
    plugin, problems = _validate_file(path, scope)
    return path, plugin, problems


def read_source(cwd: Path | str | None, plugin_id: str) -> tuple[Path, str, list[Problem]]:
    path, scope = locate(cwd, plugin_id)
    text = path.read_text(encoding="utf-8")
    _plugin, problems = _validate_file(path, scope)
    return path, text, problems


def save_source(
    cwd: Path | str | None, plugin_id: str, text: str
) -> tuple[Path, AuthoredPlugin | None, list[Problem]]:
    """Write first, validate second, return the problems. Never refuses."""
    path, scope = locate(cwd, plugin_id)
    path.write_text(text, encoding="utf-8")
    plugin, problems = _validate_file(path, scope)
    return path, plugin, problems


def delete(cwd: Path | str | None, plugin_id: str) -> tuple[Path, Path]:
    """Move the file to ``.trash/``. Returns ``(was, now)``."""
    path, _scope = locate(cwd, plugin_id)
    trash = path.parent / TRASH_DIRNAME
    trash.mkdir(parents=True, exist_ok=True)
    target = trash / f"{path.stem}-{int(time.time())}.md"
    path.replace(target)
    return path, target


def locate(cwd: Path | str | None, plugin_id: str) -> tuple[Path, str]:
    """The file behind an authored id, project scope winning."""
    for scope in ("project", "user"):
        try:
            directory = scope_dir(cwd, scope)
        except AuthoringError:
            continue
        for path in _md_files(directory):
            doc = parse_document(_read(path))
            kind = doc.meta.get("kind", "").strip().lower()
            name = doc.meta.get("name", "").strip() or path.stem
            if f"{kind}.{name}" == plugin_id:
                return path, scope
    raise AuthoringError(
        f"no authored plugin {plugin_id!r}",
        fix="Check the id, or list the authored plugins first.", status=404)


def _md_files(directory: Path) -> list[Path]:
    try:
        if not directory.is_dir():
            return []
        return sorted(p for p in directory.glob("*.md") if not p.name.startswith("."))
    except OSError:
        return []


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _validate_file(path: Path, scope: str) -> tuple[AuthoredPlugin | None, list[Problem]]:
    text = _read(path)
    doc = parse_document(text)
    return schema.validate(doc, scope=scope, path=str(path),
                           default_name=path.stem, source_text=text)


def validate_text(
    text: str, *, kind: str = "", scope: str = "project", name: str = "draft",
) -> tuple[AuthoredPlugin | None, list[Problem]]:
    """Validate a draft that is not on disk. Writes nothing."""
    doc = parse_document(text)
    if kind and not doc.meta.get("kind"):
        doc.meta["kind"] = kind
    return schema.validate(doc, scope=scope, path="", default_name=name,
                           source_text=text)


# --------------------------------------------------------------------------
# duplicate-to-customise
# --------------------------------------------------------------------------

# Why a kind cannot be duplicated. Each of these is shown to the user in place
# of the button, with the recourse that *is* available.
#
# The table is queryable through ``refusal()`` rather than only reachable by
# pressing the button and reading a 400, because a button that exists in order
# to fail is worse than a sentence saying what to press instead.
_NOT_DUPLICABLE: dict[str, tuple[str, str]] = {
    "tool": (
        "a built-in tool is Python: its schema, its argument validation and its "
        "permission shape all come from the class the runtime instantiates, and "
        "none of that is expressible as an argv template. A copy would be a "
        "file that claims to be this tool and is not.",
        "Use New command tool instead -- a command tool is a narrowed shell "
        "command with a name, typed parameters and an approval prompt that "
        "shows the exact argv.",
    ),
    "provider": (
        "a provider is a wire-protocol adapter: streaming, tool-call framing "
        "and usage accounting. There is no data shape for that which is not "
        "Python with extra steps.",
        "Providers are added as Python packages through the entry-point "
        "mechanism.",
    ),
    "mcp_server": (
        "MCP servers are configured through the 'mcpServers' block in "
        "settings.json in this version.",
        "Edit settings.json, then trust the project so its servers may start.",
    ),
    "policy": ("nothing consumes a second permission policy.",
               "Permission rules are authorable in settings.json."),
    "hook": ("nothing consumes a second copy of a loop hook: it would be inert, "
             "and an inert plugin sitting in the list looking enabled is worse "
             "than no button.", "Adjust its settings instead."),
    "storage": ("the session log format is fixed by contract.",
                "Read the format on the plugin's card."),
    "panel": ("a panel is frontend code.", "Nothing to duplicate."),
}

# One id deserves its own sentence, because it is the one people will try:
# a command tool *is* a narrowed bash, so "duplicate bash" is a reasonable
# thing to reach for and the generic tool refusal answers the wrong question.
_NOT_DUPLICABLE_ID: dict[str, tuple[str, str]] = {
    "tool.bash": (
        "bash's behaviour is 'run whatever the model wrote', and a copy of "
        "that is not a narrower tool -- it is the same tool under a second "
        "name, with the same reach and a name that hides it.",
        "Use New command tool instead. It pins one command down to an argv "
        "template with typed parameters, which is what a copy of bash is "
        "usually reached for; a pipeline still belongs to bash itself, "
        "because a command tool is argv-only and never sees a shell.",
    ),
}

_NOTHING_TO_COPY = ("this plugin has nothing a file could hold.",
                    "Nothing to duplicate.")


def refusal(plugin_id: str) -> tuple[str, str] | None:
    """``(reason, recourse)`` when ``plugin_id`` cannot be duplicated.

    The static half of the table: it answers from the id alone, so a caller
    can render the reason in place of the button. It deliberately does **not**
    look at the filesystem, which means it says "no" for a kind whose internal
    plugins are not duplicable even when an *authored* plugin of that kind
    would be -- authored anything is a byte copy and always allowed, and
    ``duplicate`` checks for that first.
    """
    kind = _kind_of(plugin_id)
    if kind in ("agent", "prompt"):
        return None
    return _NOT_DUPLICABLE_ID.get(plugin_id) or _NOT_DUPLICABLE.get(kind, _NOTHING_TO_COPY)


def duplicate(
    cwd: Path | str | None,
    plugin_id: str,
    *,
    scope: str = "project",
    name: str = "",
    bodies: dict[str, str] | None = None,
) -> tuple[Path, AuthoredPlugin | None, list[Problem]]:
    """Materialise an editable copy of ``plugin_id``. Raises on a refusal."""
    if scope not in SCOPES:
        raise AuthoringError(f"'{scope}' is not a scope", fix="Use 'user' or 'project'.")
    directory = scope_dir(cwd, scope)
    directory.mkdir(parents=True, exist_ok=True)

    kind, _, original = plugin_id.partition(".")
    if not original:
        raise AuthoringError(f"{plugin_id!r} is not a plugin id",
                             fix="Ids look like 'agent.explore'.", status=404)

    authored = discover(cwd).get(plugin_id)
    if authored is not None:
        text = authored.source_text or _read(Path(authored.path))
        base = _slugify(name) or authored.name
        slug = allocate_name(directory, base, kind=authored.kind,
                             copy=not name)
        text = _rewrite_identity(text, slug, f"{authored.display_title} (copy)",
                                 plugin_id)
        return _write_copy(directory, slug, text, scope)

    if kind == "agent":
        return _duplicate_agent(directory, original, plugin_id, name, scope)
    if kind == "prompt":
        return _duplicate_section(directory, plugin_id, name, scope, bodies or {})

    reason, recourse = refusal(plugin_id) or _NOTHING_TO_COPY
    raise AuthoringError(f"{plugin_id} cannot be duplicated: {reason}",
                         fix=recourse, status=400, code=schema.NOT_DUPLICABLE)


def _kind_of(plugin_id: str) -> str:
    prefix = plugin_id.split(".", 1)[0]
    return {
        "tool": "tool", "prompt": "prompt", "agent": "agent", "mcp": "mcp_server",
        "provider": "provider", "runtime": "hook", "hook": "hook",
        "policy": "policy", "storage": "storage", "panel": "panel",
    }.get(prefix, prefix)


def _duplicate_agent(
    directory: Path, original: str, plugin_id: str, name: str, scope: str,
) -> tuple[Path, AuthoredPlugin | None, list[Problem]]:
    from quickcode.subagents.definitions import builtin_defs

    defn = builtin_defs().get(original)
    if defn is None:
        raise AuthoringError(f"no agent {original!r} to duplicate",
                             fix="Check the id.", status=404)
    slug = allocate_name(directory, _slugify(name) or original, kind="agent",
                         copy=not name)
    tools = defn.tools
    lines = [
        "---",
        "kind: agent",
        f"name: {slug}",
        f"title: {original.capitalize()} (copy)",
        f"description: {defn.description.strip()}",
        "group: Agents",
    ]
    if tools is not None:
        lines.append(f"tools: [{', '.join(tools)}]")
    lines += [
        f"model: {defn.model}",
        f"models: [{', '.join(defn.models)}]" if defn.models else "models: []",
        f"model_selectable: {'true' if defn.model_selectable else 'false'}",
        f"mode_cap: {defn.mode_cap.value}",
        f"max_turns: {defn.max_turns}",
        f"color: {defn.color}",
        f"skip_project_instructions: "
        f"{'true' if defn.skip_project_instructions else 'false'}",
        f"derived_from: {plugin_id}",
        "---",
        "",
        defn.prompt_body.strip(),
        "",
    ]
    return _write_copy(directory, slug, "\n".join(lines), scope)


def _duplicate_section(
    directory: Path, plugin_id: str, name: str, scope: str, bodies: dict[str, str],
) -> tuple[Path, AuthoredPlugin | None, list[Problem]]:
    from quickcode.prompts import sections as prompt_sections

    section = prompt_sections.get(plugin_id)
    if section is None:
        raise AuthoringError(f"no prompt section {plugin_id!r} to duplicate",
                             fix="Check the id.", status=404)
    base = _slugify(name) or _slugify(plugin_id.split(".", 1)[1])
    slug = allocate_name(directory, base, kind="prompt", copy=not name)
    locked = section.tier == "locked" or section.generated
    body = "" if locked else bodies.get(plugin_id, "")
    if not body:
        body = (f"<{slug}>\nYour text runs after {section.title.lower()}. The "
                f"original still renders -- you are adding a voice, not "
                f"replacing one.\n</{slug}>")
    lines = [
        "---",
        "kind: prompt",
        f"name: {slug}",
        f"title: {section.title} (copy)",
        f"description: {section.description.strip()}",
        "group: Prompt",
        f"after: {plugin_id}",
        "applies_to: [main]",
        "when: always",
        f"derived_from: {plugin_id}",
        "---",
        "",
        body.strip(),
        "",
    ]
    return _write_copy(directory, slug, "\n".join(lines), scope)


def _write_copy(
    directory: Path, slug: str, text: str, scope: str,
) -> tuple[Path, AuthoredPlugin | None, list[Problem]]:
    path = directory / f"{slug}.md"
    if path.exists():
        raise AuthoringError(f"{path.name} already exists",
                             fix="Pick another name.", status=409,
                             code=schema.ID_DUPLICATE)
    path.write_text(text, encoding="utf-8")
    plugin, problems = _validate_file(path, scope)
    return path, plugin, problems


_IDENTITY_KEYS = ("name", "title", "derived_from")


def _rewrite_identity(text: str, slug: str, title: str, derived_from: str) -> str:
    """Retarget a byte copy's identity keys without reformatting the file."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), -1)
    if close < 0:
        return text
    replacements = {"name": slug, "title": title, "derived_from": derived_from}
    seen: set[str] = set()
    for i in range(1, close):
        key = lines[i].split(":", 1)[0].strip()
        if key in _IDENTITY_KEYS and key not in seen:
            lines[i] = f"{key}: {replacements[key]}"
            seen.add(key)
    for key in _IDENTITY_KEYS:
        if key not in seen:
            lines.insert(close, f"{key}: {replacements[key]}")
            close += 1
    return "\n".join(lines)


def problems_json(problems: list[Problem]) -> list[dict]:
    return [p.to_json() for p in problems]


def plugin_json(plugin: AuthoredPlugin) -> dict:
    return {
        "id": plugin.id,
        "kind": plugin.kind,
        "name": plugin.name,
        "title": plugin.display_title,
        "description": plugin.description,
        "group": plugin.group,
        "scope": plugin.scope,
        "path": plugin.path,
        "derived_from": plugin.derived_from,
        "enabled_by_default": plugin.enabled_by_default,
    }
