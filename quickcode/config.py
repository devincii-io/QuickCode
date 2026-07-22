"""Configuration: profiles (base_url, api key env, model roles) + project env.

User config lives at ``~/.quickcode/config.json``. A profile names a provider
endpoint and the orchestrator/worker model defaults. Everything here is loaded
once at startup and is stable for the session.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
CONFIG_DIR = Path.home() / ".quickcode"
CONFIG_PATH = CONFIG_DIR / "config.json"

# The default (warm, non-blue) theme. Every key is a user-editable color; the
# Theme tab in Settings edits these live and persists them here. Kept as a
# plain dict so ``config`` has no dependency on the UI layer.
DEFAULT_THEME_COLORS: dict[str, str] = {
    "background": "#15140f",
    "surface": "#1b1a15",
    "panel": "#24221b",
    "primary": "#c8973f",
    "secondary": "#8fae74",
    "accent": "#e08a4b",
    "foreground": "#e9e3d5",
    "success": "#8fb56a",
    "warning": "#d9a441",
    "error": "#d76c53",
    "boost": "#2b2820",
}

# Order to present color fields in the editor (stable, grouped structure→accent).
THEME_COLOR_ORDER: list[str] = [
    "background",
    "surface",
    "panel",
    "boost",
    "foreground",
    "primary",
    "secondary",
    "accent",
    "success",
    "warning",
    "error",
]


Tier = str  # "quality" | "balanced" | "cheap"
Role = str  # "orchestrator" | "worker"


@dataclass
class CatalogEntry:
    """One curated model the user has admitted into this project's context.

    The ``agent`` tool selects a model by tier/role against this catalog, so a
    delegation can say "spawn a cheap worker" without hardcoding a slug.
    """

    id: str  # OpenRouter slug, e.g. "anthropic/claude-sonnet-4.5"
    tier: Tier = "balanced"
    roles: list[Role] = field(default_factory=lambda: ["worker"])
    label: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "tier": self.tier, "roles": self.roles, "label": self.label}

    @classmethod
    def from_dict(cls, d: dict) -> CatalogEntry:
        return cls(
            id=d["id"],
            tier=d.get("tier", "balanced"),
            roles=d.get("roles", ["worker"]),
            label=d.get("label", ""),
        )


@dataclass
class Profile:
    name: str = "default"
    base_url: str = DEFAULT_BASE_URL
    orchestrator_model: str = "anthropic/claude-opus-4.8"
    worker_model: str = "anthropic/claude-sonnet-4.5"
    # Curated OpenRouter models admitted into this profile (empty = allow any).
    catalog: list[CatalogEntry] = field(default_factory=list)

    @property
    def api_key_env(self) -> str:
        """Fixed, non-configurable env var name for the API key."""
        from quickcode.secrets import API_KEY_ENV

        return API_KEY_ENV

    @property
    def api_key(self) -> str | None:
        """Env var first, then the DPAPI-encrypted value saved from Settings."""
        from quickcode.secrets import load_api_key

        return load_api_key()

    def models_for(self, role: Role, tier: Tier | None = None) -> list[CatalogEntry]:
        """Curated models eligible for a role, optionally filtered by tier."""
        out = [e for e in self.catalog if role in e.roles]
        if tier:
            out = [e for e in out if e.tier == tier]
        return out

    def resolve(self, role: Role, tier: Tier | None = None) -> str:
        """Pick a model slug for a role/tier: first catalog match, else the
        configured role default."""
        matches = self.models_for(role, tier)
        if matches:
            return matches[0].id
        return self.orchestrator_model if role == "orchestrator" else self.worker_model


@dataclass
class Config:
    active_profile: str = "default"
    profiles: dict[str, Profile] = field(default_factory=lambda: {"default": Profile()})
    default_mode: str = "ask"
    theme: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_THEME_COLORS))

    @property
    def profile(self) -> Profile:
        return self.profiles.get(self.active_profile, Profile())

    def theme_colors(self) -> dict[str, str]:
        """Full color map: defaults overlaid with any saved user overrides."""
        merged = dict(DEFAULT_THEME_COLORS)
        merged.update({k: v for k, v in (self.theme or {}).items() if v})
        return merged

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> Config:
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        profiles: dict[str, Profile] = {}
        for name, p in raw.get("profiles", {}).items():
            profiles[name] = Profile(
                name=name,
                base_url=p.get("base_url", DEFAULT_BASE_URL),
                orchestrator_model=p.get("orchestrator_model", "anthropic/claude-opus-4.8"),
                worker_model=p.get("worker_model", "anthropic/claude-sonnet-4.5"),
                catalog=[CatalogEntry.from_dict(e) for e in p.get("catalog", [])],
            )
        if not profiles:
            profiles = {"default": Profile()}
        theme = dict(DEFAULT_THEME_COLORS)
        saved_theme = raw.get("theme")
        if isinstance(saved_theme, dict):
            theme.update({k: v for k, v in saved_theme.items() if isinstance(v, str) and v})
        return cls(
            active_profile=raw.get("active_profile", next(iter(profiles))),
            profiles=profiles,
            default_mode=raw.get("default_mode", "ask"),
            theme=theme,
        )

    def save(self, path: Path = CONFIG_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "active_profile": self.active_profile,
            "default_mode": self.default_mode,
            "theme": self.theme,
            "profiles": {
                name: {
                    "base_url": p.base_url,
                    "orchestrator_model": p.orchestrator_model,
                    "worker_model": p.worker_model,
                    "catalog": [e.to_dict() for e in p.catalog],
                }
                for name, p in self.profiles.items()
            },
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


@dataclass
class Environment:
    """Per-session facts spliced into the system prompt. Frozen at startup."""

    cwd: str
    platform: str
    os_version: str
    shell_name: str
    session_date: str
    is_git_repo: bool
    git_branch: str
    project_instructions: str = ""
    instructions_file: str = ""

    @classmethod
    def detect(cls, cwd: Path | None = None) -> Environment:
        import datetime

        root = Path(cwd or Path.cwd())
        shell = _detect_shell()
        is_git = (root / ".git").exists()
        branch = ""
        if is_git:
            try:
                branch = subprocess.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    cwd=root, capture_output=True, text=True, timeout=5,
                ).stdout.strip()
            except Exception:
                branch = ""
        instr, instr_file = _load_project_instructions(root)
        return cls(
            cwd=str(root),
            platform=platform.system(),
            os_version=platform.version(),
            shell_name=shell,
            session_date=datetime.date.today().isoformat(),
            is_git_repo=is_git,
            git_branch=branch,
            project_instructions=instr,
            instructions_file=instr_file,
        )


def _detect_shell() -> str:
    if platform.system() == "Windows":
        for candidate in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"):
            if Path(candidate).exists():
                return "bash"
        return "powershell"
    return os.environ.get("SHELL", "/bin/bash").split("/")[-1]


def _load_project_instructions(root: Path) -> tuple[str, str]:
    for name in ("QUICKCODE.md", "AGENTS.md", "CLAUDE.md"):
        p = root / name
        if p.exists():
            try:
                return p.read_text(encoding="utf-8"), name
            except Exception:
                continue
    return "", ""
