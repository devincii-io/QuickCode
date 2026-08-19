"""Project trust gate — the security decision behind AUTHORING.md §5.4.

Cloning a repository and opening it in QuickCode would otherwise hand that
repository the ability to spawn processes: a committed
``.quickcode/settings.json`` that declares ``mcpServers`` is arbitrary command
execution the moment the directory is opened. This module makes that config
**inert until the project is explicitly trusted once**.

Design constraints (all enforced here):

* Trust is recorded at **user scope** (``~/.quickcode/trust.json``), never
  inside the project — a project cannot declare itself trusted, because we only
  ever read the user-scope store.
* Trust is bound to a **hash over the security-relevant project config**: the
  project-scope ``mcpServers`` blocks, the project's authored command-tool
  files, *and* the policy config below. A later edit that adds or changes any
  of them changes the hash, so the old grant no longer matches and the project
  re-prompts. They are the same risk wearing different clothes — a committed
  file that widens what the agent may do on its own — so one grant must not
  cover a later edit to another.
* **Untrusted is the default** for any path not already recorded — including
  paths that happen to sit in the recent-projects list. Nothing is
  grandfathered.
* Trust can be **revoked**.

The gate covers two kinds of project-scope config. **Executable config** names a
program to run: ``mcpServers`` blocks and ``kind: tool`` plugin files. **Policy
config** widens what the agent may do without being asked: a ``permissions``
allowlist, a ``default_mode``. Neither spawns anything by itself, but a
committed ``default_mode: "yolo"`` hands a cloned repository the same thing a
committed MCP server does, one step later, so both gate under the one grant.

User-scope ``~/.quickcode/settings.json`` servers are deliberately *not* gated:
they are the user's own files, there is no attacker to defend against, and
prompting for them trains the reflex that makes the project prompt worthless.
Only project-scope config passes through this gate.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from quickcode.config import CONFIG_DIR

log = logging.getLogger("quickcode.security.trust")

TRUST_FILENAME = "trust.json"
STORE_VERSION = 1

# Project-scope files that may carry executable or policy config. Kept in one
# place so the hash and the loaders can never disagree about what "the project
# config" is.
PROJECT_SETTINGS_FILES = (
    Path(".quickcode") / "settings.json",
    Path(".quickcode") / "settings.local.json",
)

# Authored plugins live here. A ``kind: tool`` file names a program and an argv,
# so it is executable config in exactly the way an mcpServers block is.
PROJECT_PLUGINS_DIR = Path(".quickcode") / "plugins"

_KIND_RE = re.compile(r"^kind\s*:\s*[\"']?([A-Za-z][A-Za-z0-9_-]*)", re.MULTILINE)

# The policy half of what this gate governs, named once so the hash, the report
# and the three loaders that drop it can never disagree about the list.
#
# Only the *widening* direction is here. ``permissions.deny`` and
# ``permissions.ask`` narrow, and a project that can only narrow needs nobody's
# consent -- refusing those would itself be a way to widen. The same reasoning
# keeps ``runtime.subagents`` and the compaction knobs out: they are resource
# limits, already clamped to the range their own card declares, and no value of
# them lets the agent touch anything it could not touch before.
GATED_RULE_KINDS = ("allow",)
GATED_PLUGIN_IDS = ("runtime.permissions",)
GATED_PRESET_FIELDS = ("default_mode",)

# The starting modes an untrusted project may still ask for. Listed as what is
# permitted rather than what is refused so that a mode added later is refused
# until somebody decides otherwise, which is the direction to be wrong in.
#
# A repository asking for ``plan`` is asking the agent to do *less* on its own,
# and a repository that ships "open me in plan mode" is a repository being
# careful. Everything above ``ask`` -- ``auto-edit``, ``dontask``, ``yolo`` --
# is the boundary moving outward, and that is the grant this gate exists for.
GRANTABLE_MODES = frozenset({"plan", "ask"})


def project_may_state(key: str, value: Any) -> bool:
    """Whether an untrusted project may still state this policy value.

    ``default_mode`` is the one gated value with a safe direction, because
    modes are ordered and the ordering is the whole point of them. Nothing else
    is: an allow rule and a policy knob have no "narrower" reading that can be
    computed here, and inventing one per key is how the second such key gets it
    wrong. So everything else answers no, including keys added later.
    """
    if key == "default_mode":
        return isinstance(value, str) and value in GRANTABLE_MODES
    return False


def trust_path() -> Path:
    return CONFIG_DIR / TRUST_FILENAME


def _norm(path: str | os.PathLike[str]) -> str:
    """Normalized key for a project directory.

    Matches ``server.projects.project_id`` normalization: forward slashes and,
    on Windows, case-folded — so ``C:\\Proj`` and ``c:\\proj`` share one grant.
    """
    norm = str(Path(path).resolve()).replace("\\", "/")
    if os.name == "nt":
        norm = norm.casefold()
    return norm


def _project_settings(cwd: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Both project settings files, parsed, unreadable ones skipped.

    One reader for both of them, so nothing that gates on their contents can
    end up looking at a different pair of files than the hash does.
    """
    out: list[dict[str, Any]] = []
    root = Path(cwd)
    for rel in PROJECT_SETTINGS_FILES:
        try:
            data = json.loads((root / rel).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def project_mcp_servers(cwd: str | os.PathLike[str]) -> dict[str, dict[str, Any]]:
    """Merged ``mcpServers`` declared by the project's own settings files.

    This is exactly the executable-bearing project-scope config: the set of
    server specs the trust gate governs. User-scope config is intentionally
    excluded.
    """
    merged: dict[str, dict[str, Any]] = {}
    for data in _project_settings(cwd):
        servers = data.get("mcpServers")
        if isinstance(servers, dict):
            for name, spec in servers.items():
                if isinstance(spec, dict) and isinstance(spec.get("command"), str):
                    merged[str(name)] = spec
    return merged


def _declared_kind(text: str) -> str | None:
    """The ``kind:`` an authored plugin file declares, or ``None`` if unreadable.

    This reads the frontmatter directly instead of calling the real parser
    because ``kernel.authoring.discovery`` imports *this* module: security sits
    below the kernel and cannot import it back. Only enough is read to answer
    one question — is this a command tool — and ``None`` means "could not tell",
    which the caller resolves the safe way.
    """
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    match = _KIND_RE.search(text[:end])
    return match.group(1).lower() if match else None


def project_command_tools(cwd: str | os.PathLike[str]) -> dict[str, str]:
    """``filename -> sha256`` for each project plugin file that can execute.

    Hashing the file's bytes rather than its parsed argv is deliberate: a
    change anywhere in a command tool's definition — the argv, a default, the
    working directory it runs in — is a change to what approving it means.

    A file whose kind cannot be read is **included**. The unreadable case is
    the one an attacker controls, and the safe reading of a declaration we
    cannot parse is the one that re-prompts.
    """
    directory = Path(cwd) / PROJECT_PLUGINS_DIR
    out: dict[str, str] = {}
    try:
        if not directory.is_dir():
            return out
        # Never recursive: .trash/ lives underneath and holds deleted files.
        files = sorted(directory.glob("*.md"))
    except OSError:
        return out
    for path in files:
        if path.name.startswith("."):
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        kind = _declared_kind(raw.decode("utf-8", errors="replace"))
        if kind is not None and kind != "tool":
            continue  # agents and prompt sections are text; they are not gated
        out[path.name] = hashlib.sha256(raw).hexdigest()
    return out


def project_policy_config(cwd: str | os.PathLike[str]) -> dict[str, Any]:
    """The project-scope config that widens the permission boundary.

    Only what an untrusted project may *not* state: a ``default_mode`` of
    ``plan`` is left out because it is honoured either way, and a report that
    named it would be claiming a refusal that never happened.

    Keyed by the dotted path it was written at (``permissions.allow``,
    ``plugins.runtime.permissions.default_mode``) for two reasons: the trust
    report can then name what it is refusing in the words the file uses, and a
    grant is bound to the *values*, so adding ``default_mode: "yolo"`` to a
    project that was trusted for its MCP servers re-prompts instead of
    inheriting that approval.
    """
    out: dict[str, Any] = {}
    for data in _project_settings(cwd):
        perms = data.get("permissions")
        if isinstance(perms, dict):
            for kind in GATED_RULE_KINDS:
                rules = perms.get(kind)
                if isinstance(rules, list) and rules:
                    out.setdefault(f"permissions.{kind}", []).extend(
                        str(r) for r in rules
                    )
        plugins = data.get("plugins")
        if isinstance(plugins, dict):
            for plugin_id in GATED_PLUGIN_IDS:
                entry = plugins.get(plugin_id)
                settings = entry.get("settings") if isinstance(entry, dict) else None
                if isinstance(settings, dict):
                    for key, value in settings.items():
                        if not project_may_state(key, value):
                            out[f"plugins.{plugin_id}.{key}"] = value
        presets = data.get("presets")
        if isinstance(presets, dict):
            for name, body in presets.items():
                if not isinstance(body, dict):
                    continue
                for field_name in GATED_PRESET_FIELDS:
                    value = body.get(field_name)
                    if value and not project_may_state(field_name, value):
                        out[f"presets.{name}.{field_name}"] = value
        # Profiles are the third widening surface. The loader in core.profiles
        # gates each field independently, so an untrusted project can never
        # widen -- but without this a project already trusted for its MCP
        # servers could *add* a permissive profile afterwards, leaving the hash
        # unchanged and the grant silently covering it. The shape of a profile
        # lives in core.profiles, and the function-local import keeps security
        # below the kernel, as state._project_entries does.
        from quickcode.core.profiles import policy_keys_from_settings

        out.update(policy_keys_from_settings(data))
    return out


def config_hash(cwd: str | os.PathLike[str]) -> str:
    """Stable SHA-256 over the project's security-relevant config.

    Empty config hashes deterministically too; the value only ever matters when
    there is something to gate, but a stable hash keeps ``is_trusted`` total.

    The payload is keyed rather than concatenated so that adding a further kind
    of gated config later cannot collide with an existing grant -- which is
    exactly what ``policy`` is.
    """
    payload: dict[str, Any] = {
        "mcpServers": project_mcp_servers(cwd),
        "commandTools": project_command_tools(cwd),
    }
    # Added under its own key, and only when the project declares any: a
    # project that has none hashes exactly as it did before policy config was
    # gated, so grants already recorded on disk stay valid. A project that
    # gains one gets a new hash, which is the re-prompt.
    policy = project_policy_config(cwd)
    if policy:
        payload["policy"] = policy
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class TrustStatus:
    """What the frontend needs to render the trust prompt for one project."""

    trusted: bool
    has_servers: bool
    server_names: list[str]
    config_hash: str
    # True when there is executable config that is NOT trusted, i.e. something
    # was (or will be) refused. This is the "visible refusal" flag.
    inert: bool
    reason: str
    # Authored command tools the project declares, by filename. Separate from
    # servers because they are refused by a different loader and the UI names
    # them differently -- but they gate together, under one grant.
    tool_files: list[str] = field(default_factory=list)
    # Policy config the project declares, by the dotted key it is written at.
    # Nothing here runs a program; it is the half that widens what may run
    # without asking, and it gates under the same grant.
    policy_keys: list[str] = field(default_factory=list)

    @property
    def has_tools(self) -> bool:
        return bool(self.tool_files)

    @property
    def has_policy(self) -> bool:
        return bool(self.policy_keys)

    def to_json(self) -> dict[str, Any]:
        return {
            "trusted": self.trusted,
            "has_servers": self.has_servers,
            "servers": list(self.server_names),
            "has_tools": self.has_tools,
            "tools": list(self.tool_files),
            "has_policy": self.has_policy,
            "policy": list(self.policy_keys),
            "hash": self.config_hash,
            "inert": self.inert,
            "reason": self.reason,
        }


class TrustStore:
    """The user-scope record of which project paths are trusted, and for which
    config hash. Injectable path so tests never touch the real store."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else trust_path()

    # ---- persistence ----
    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": STORE_VERSION, "projects": {}}
        if not isinstance(data, dict):
            return {"version": STORE_VERSION, "projects": {}}
        projects = data.get("projects")
        if not isinstance(projects, dict):
            data["projects"] = {}
        return data

    def _save(self, data: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            log.warning("could not save trust store: %s", e)

    # ---- queries ----
    def recorded_hash(self, cwd: str | os.PathLike[str]) -> str | None:
        entry = self._load()["projects"].get(_norm(cwd))
        if isinstance(entry, dict) and isinstance(entry.get("hash"), str):
            return entry["hash"]
        return None

    def is_trusted(self, cwd: str | os.PathLike[str]) -> bool:
        """True iff this exact config (by hash) was granted for this path.

        A missing record, or a hash mismatch (the config was edited since the
        grant), both read as untrusted — that is the re-prompt.
        """
        recorded = self.recorded_hash(cwd)
        return recorded is not None and recorded == config_hash(cwd)

    def status(self, cwd: str | os.PathLike[str]) -> TrustStatus:
        servers = project_mcp_servers(cwd)
        names = sorted(servers)
        tool_files = sorted(project_command_tools(cwd))
        policy_keys = sorted(project_policy_config(cwd))
        trusted = self.is_trusted(cwd)
        has_servers = bool(servers)
        has_gated = has_servers or bool(tool_files) or bool(policy_keys)
        inert = has_gated and not trusted
        # One noun for whatever this project actually declares, so the sentence
        # is true for a project with only tools, or only settings, as well as
        # for one with only servers.
        what = " and ".join(part for part, present in (
            ("MCP servers", has_servers),
            ("command tools", bool(tool_files)),
            ("permission settings", bool(policy_keys)),
        ) if present)
        if not has_gated:
            reason = ("no project-scope MCP servers, command tools or permission "
                      "settings declared")
        elif trusted:
            reason = "project trusted for this configuration"
        elif self.recorded_hash(cwd) is not None:
            reason = (f"project configuration changed since it was trusted; "
                      f"re-approve to apply its {what}")
        else:
            reason = f"project not trusted; its {what} are inert until you approve them"
        return TrustStatus(
            trusted=trusted,
            has_servers=has_servers,
            server_names=names,
            config_hash=config_hash(cwd),
            inert=inert,
            reason=reason,
            tool_files=tool_files,
            policy_keys=policy_keys,
        )

    # ---- mutations ----
    def grant(self, cwd: str | os.PathLike[str]) -> str:
        """Trust this project for its current config. Returns the bound hash."""
        h = config_hash(cwd)
        data = self._load()
        data.setdefault("version", STORE_VERSION)
        data["projects"][_norm(cwd)] = {
            "path": str(Path(cwd).resolve()),
            "hash": h,
            "granted_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        self._save(data)
        return h

    def revoke(self, cwd: str | os.PathLike[str]) -> bool:
        """Forget any grant for this project. Returns True if one existed.

        Revocation governs future connects: the next time the project's MCP
        servers would be spawned they are refused again. Processes already
        running in an open session are owned by the ProjectHub and are not
        killed here; they end when that project/session is torn down.
        """
        data = self._load()
        existed = data["projects"].pop(_norm(cwd), None) is not None
        if existed:
            self._save(data)
        return existed


# ---- module-level convenience over the default (user-scope) store ----

def default_store() -> TrustStore:
    return TrustStore()


def is_trusted(cwd: str | os.PathLike[str]) -> bool:
    return default_store().is_trusted(cwd)


def resolve_trust(cwd: str | os.PathLike[str] | None, override: bool | None = None) -> bool:
    """The gate's answer for a config loader, with the caller's override.

    ``override`` is what tests pass so nothing reads the real user-scope store;
    leave it ``None`` in production. Never raises, and every way of failing to
    get an answer -- no cwd, an unreadable store, an import that went wrong --
    reads as untrusted, because that is the direction a config loader can be
    wrong in safely.
    """
    if override is not None:
        return override
    if cwd is None:
        return False
    try:
        return is_trusted(cwd)
    except Exception:
        return False


def status(cwd: str | os.PathLike[str]) -> TrustStatus:
    return default_store().status(cwd)


def reaffirm(cwd: str | os.PathLike[str], *, store: TrustStore | None = None) -> bool:
    """Re-bind an existing grant to this project's config as it is now.

    Only ever a re-bind: the caller has to have established that the project
    was trusted *before* whatever it just changed, because by the time the file
    is written the hash no longer matches and `status()` already says False.
    Use :func:`keep_trust`, which handles that ordering.
    """
    store = store or default_store()
    try:
        store.grant(cwd)
    except Exception:  # never let bookkeeping break the write it follows
        return False
    return True


@contextlib.contextmanager
def keep_trust(cwd: str | os.PathLike[str], *, store: TrustStore | None = None):
    """Let this app change a project's config without untrusting the project.

    The trust hash covers ``.quickcode/settings.json`` and
    ``settings.local.json``, so *any* write to either invalidates it -- and the
    app writes to both on the user's behalf. Clicking "Always allow" appended a
    rule to settings.local.json and thereby untrusted the project, which meant
    that rule, and every allow rule saved before it, was ignored from the next
    session on, and the project's MCP servers went inert. The user had answered
    a permission prompt; nothing about that says "and stop trusting this".

    Trust is read *before* the block and re-bound after, because afterwards the
    hash has already moved. A project that was not trusted stays untrusted, so
    this can never grant trust by a side door, and an edit made outside the app
    still invalidates the hash exactly as it did before.
    """
    store = store or default_store()
    try:
        was_trusted = store.status(cwd).trusted
    except Exception:
        was_trusted = False
    try:
        yield
    finally:
        if was_trusted:
            reaffirm(cwd, store=store)
