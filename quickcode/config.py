"""Configuration: profiles (base_url, api key env, model roles) + project env.

User config lives at ``~/.quickcode/config.json``. A profile names a provider
endpoint and the orchestrator/worker model defaults. Everything here is loaded
once at startup and is stable for the session.
"""

from __future__ import annotations

import json
import os
import platform
from dataclasses import dataclass, field
from pathlib import Path

from quickcode import subproc
from quickcode.search import SearchSettings

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

# Ready-to-use palettes for people who don't want to tune eleven hex fields.
# The warm palette remains the default and every preset is still editable.
#
# Structural tokens (background / surface / panel / boost) stay neutral in the
# dark presets on purpose: a hue baked into every surface reads as a colour
# cast across the whole app. Colour belongs to primary / secondary / accent,
# where it marks something, not to the paper it is printed on.
THEME_PRESETS: dict[str, dict[str, str]] = {
    "warm": dict(DEFAULT_THEME_COLORS),
    "dark": {
        "background": "#101011", "surface": "#18181a", "panel": "#212124",
        "primary": "#58a6ff", "secondary": "#3fb950", "accent": "#d2a8ff",
        "foreground": "#e6e6e8", "success": "#3fb950", "warning": "#d29922",
        "error": "#f85149", "boost": "#2b2b30",
    },
    "midnight": {
        "background": "#0c0c0e", "surface": "#141416", "panel": "#1d1d21",
        "primary": "#7aa2f7", "secondary": "#9ece6a", "accent": "#bb9af7",
        "foreground": "#e3e3e6", "success": "#9ece6a", "warning": "#e0af68",
        "error": "#f7768e", "boost": "#27272d",
    },
    "light": {
        "background": "#f6f8fa", "surface": "#ffffff", "panel": "#eaeef2",
        "primary": "#0969da", "secondary": "#1a7f37", "accent": "#8250df",
        "foreground": "#1f2328", "success": "#1a7f37", "warning": "#9a6700",
        "error": "#cf222e", "boost": "#d8dee4",
    },
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


# Model cost band. Deliberately NOT called ``Tier``: ``kernel/spec.py`` owns
# that name for mutability (free | confirm | locked), and one word meaning
# two things across modules that import both is a bug waiting for a reader.
ModelTier = str  # "quality" | "balanced" | "cheap"
Role = str  # "orchestrator" | "worker"


@dataclass
class CatalogEntry:
    """One curated model the user has admitted into this project's context.

    The ``agent`` tool selects a model by tier/role against this catalog, so a
    delegation can say "spawn a cheap worker" without hardcoding a slug.
    """

    id: str  # OpenRouter slug, e.g. "anthropic/claude-sonnet-4.5"
    tier: ModelTier = "balanced"
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
    # Provider plugin name (entry-point group "quickcode.providers").
    provider: str = "openai-compat"
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

    def models_for(self, role: Role, tier: ModelTier | None = None) -> list[CatalogEntry]:
        """Curated models eligible for a role, optionally filtered by tier."""
        out = [e for e in self.catalog if role in e.roles]
        if tier:
            out = [e for e in out if e.tier == tier]
        return out

    def resolve(self, role: Role, tier: ModelTier | None = None) -> str:
        """Pick a model slug for a role/tier: first catalog match, else the
        configured role default."""
        matches = self.models_for(role, tier)
        if matches:
            return matches[0].id
        return self.orchestrator_model if role == "orchestrator" else self.worker_model


# What a turn may generate, unless the user says otherwise. Deliberately well
# under a modern model's output window: the provider reserves credit against
# this, not against what the answer turns out to cost.
DEFAULT_MAX_TOKENS = 16384
MAX_MAX_TOKENS = 200_000


def _int_or(value: object, fallback: int) -> int:
    """A saved number, clamped -- config.json is hand-editable."""
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return max(0, min(n, MAX_MAX_TOKENS))


def _float_or_none(value: object) -> float | None:
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return max(0.0, min(f, 2.0))


@dataclass
class Config:
    active_profile: str = "default"
    profiles: dict[str, Profile] = field(default_factory=lambda: {"default": Profile()})
    default_mode: str = "ask"
    # Last model picked via F2; used as the session default so a switch persists.
    last_model: str = ""
    theme: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_THEME_COLORS))
    # Which web-search backend answers, and its non-secret per-provider settings.
    # Install-wide rather than per-profile: a profile is a *model* endpoint, and
    # nothing about which search engine you use follows from which model answers.
    search: SearchSettings = field(default_factory=SearchSettings)
    # The response budget sent with every request. OpenRouter reserves credit
    # against this number, so an account with a small balance is refused with a
    # 402 that names it -- "lower max_tokens ... to fit your remaining balance"
    # -- and until this was settable there was nowhere to do that. 0 means "do
    # not send one", which lets the provider apply its own default.
    max_tokens: int = DEFAULT_MAX_TOKENS
    # None keeps the provider's default rather than pinning one here.
    temperature: float | None = None
    # Whether yolo mode can be entered at all. It used to be reachable only by
    # launching with ``--yolo``, which meant it was unreachable from the
    # installed app -- nobody starts a desktop shortcut with a flag -- and a
    # permission profile asking for ``mode: yolo`` was silently rewritten to
    # ``ask`` with nothing said. Arming it here is still a deliberate act the
    # user has to perform once; it just has a door now.
    allow_yolo: bool = False

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
                provider=p.get("provider", "openai-compat"),
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
            last_model=raw.get("last_model", ""),
            theme=theme,
            search=SearchSettings.from_dict(raw.get("search")),
            max_tokens=_int_or(raw.get("max_tokens"), DEFAULT_MAX_TOKENS),
            temperature=_float_or_none(raw.get("temperature")),
            allow_yolo=bool(raw.get("allow_yolo", False)),
        )

    def save(self, path: Path = CONFIG_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "active_profile": self.active_profile,
            "default_mode": self.default_mode,
            "last_model": self.last_model,
            "theme": self.theme,
            "search": self.search.to_dict(),
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "allow_yolo": self.allow_yolo,
            "profiles": {
                name: {
                    "base_url": p.base_url,
                    "orchestrator_model": p.orchestrator_model,
                    "worker_model": p.worker_model,
                    "provider": p.provider,
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
                branch = subproc.run(
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
