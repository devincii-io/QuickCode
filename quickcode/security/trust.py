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
* Trust is bound to a **hash over the executable-bearing project config**: the
  project-scope ``mcpServers`` blocks *and* the project's authored command-tool
  files. A later edit that adds or changes either one changes the hash, so the
  old grant no longer matches and the project re-prompts. Both are the same
  risk wearing different clothes — a committed file that names a program the
  agent may run — so one grant must not cover a later edit to the other.
* **Untrusted is the default** for any path not already recorded — including
  paths that happen to sit in the recent-projects list. Nothing is
  grandfathered.
* Trust can be **revoked**.

User-scope ``~/.quickcode/settings.json`` servers are deliberately *not* gated:
they are the user's own files, there is no attacker to defend against, and
prompting for them trains the reflex that makes the project prompt worthless.
Only project-scope config passes through this gate.
"""

from __future__ import annotations

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

# Project-scope files that may carry executable config. Kept in one place so
# the hash and the loader can never disagree about what "the project config" is.
PROJECT_SETTINGS_FILES = (
    Path(".quickcode") / "settings.json",
    Path(".quickcode") / "settings.local.json",
)

# Authored plugins live here. A ``kind: tool`` file names a program and an argv,
# so it is executable config in exactly the way an mcpServers block is.
PROJECT_PLUGINS_DIR = Path(".quickcode") / "plugins"

_KIND_RE = re.compile(r"^kind\s*:\s*[\"']?([A-Za-z][A-Za-z0-9_-]*)", re.MULTILINE)


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


def project_mcp_servers(cwd: str | os.PathLike[str]) -> dict[str, dict[str, Any]]:
    """Merged ``mcpServers`` declared by the project's own settings files.

    This is exactly the executable-bearing project-scope config: the set of
    server specs the trust gate governs. User-scope config is intentionally
    excluded.
    """
    root = Path(cwd)
    merged: dict[str, dict[str, Any]] = {}
    for rel in PROJECT_SETTINGS_FILES:
        p = root / rel
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        servers = data.get("mcpServers") if isinstance(data, dict) else None
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


def config_hash(cwd: str | os.PathLike[str]) -> str:
    """Stable SHA-256 over the project's executable-bearing config.

    Empty config hashes deterministically too; the value only ever matters when
    there is something to gate, but a stable hash keeps ``is_trusted`` total.

    The payload is keyed rather than concatenated so that adding a third kind of
    executable config later cannot collide with an existing grant.
    """
    payload = {
        "mcpServers": project_mcp_servers(cwd),
        "commandTools": project_command_tools(cwd),
    }
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

    @property
    def has_tools(self) -> bool:
        return bool(self.tool_files)

    def to_json(self) -> dict[str, Any]:
        return {
            "trusted": self.trusted,
            "has_servers": self.has_servers,
            "servers": list(self.server_names),
            "has_tools": self.has_tools,
            "tools": list(self.tool_files),
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
        trusted = self.is_trusted(cwd)
        has_servers = bool(servers)
        has_executable = has_servers or bool(tool_files)
        inert = has_executable and not trusted
        # One noun for whatever this project actually declares, so the sentence
        # is true for a project with only tools as well as only servers.
        if has_servers and tool_files:
            what = "MCP servers and command tools"
        elif tool_files:
            what = "command tools"
        else:
            what = "MCP servers"
        if not has_executable:
            reason = "no project-scope MCP servers or command tools declared"
        elif trusted:
            reason = "project trusted for this configuration"
        elif self.recorded_hash(cwd) is not None:
            reason = (f"project configuration changed since it was trusted; "
                      f"re-approve to run its {what}")
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


def status(cwd: str | os.PathLike[str]) -> TrustStatus:
    return default_store().status(cwd)
