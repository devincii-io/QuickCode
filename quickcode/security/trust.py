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
* Trust is bound to a **hash over the executable-bearing project config** (the
  project-scope ``mcpServers`` blocks). A later edit that adds or changes a
  server changes the hash, so the old grant no longer matches and the project
  re-prompts.
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
from dataclasses import dataclass
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


def config_hash(cwd: str | os.PathLike[str]) -> str:
    """Stable SHA-256 over the project's executable-bearing config.

    Empty config hashes deterministically too; the value only ever matters when
    servers exist, but a stable hash keeps ``is_trusted`` total.
    """
    servers = project_mcp_servers(cwd)
    canonical = json.dumps(servers, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class TrustStatus:
    """What the frontend needs to render the trust prompt for one project."""

    trusted: bool
    has_servers: bool
    server_names: list[str]
    config_hash: str
    # True when there is executable config that is NOT trusted, i.e. servers
    # were (or will be) refused. This is the "visible refusal" flag.
    inert: bool
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {
            "trusted": self.trusted,
            "has_servers": self.has_servers,
            "servers": list(self.server_names),
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
        trusted = self.is_trusted(cwd)
        has_servers = bool(servers)
        inert = has_servers and not trusted
        if not has_servers:
            reason = "no project-scope MCP servers declared"
        elif trusted:
            reason = "project trusted for this configuration"
        elif self.recorded_hash(cwd) is not None:
            reason = "project configuration changed since it was trusted; re-approve to run its MCP servers"
        else:
            reason = "project not trusted; its MCP servers are inert until you approve them"
        return TrustStatus(
            trusted=trusted,
            has_servers=has_servers,
            server_names=names,
            config_hash=config_hash(cwd),
            inert=inert,
            reason=reason,
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
