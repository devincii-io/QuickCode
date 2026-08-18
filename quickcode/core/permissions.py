"""Permission engine: modes, rules, bash decomposition, plan-mode gate.

Two principles from docs/PERMISSIONS.md:
  1. Parse, don't prefix-match — decompose compound bash commands.
  2. Deny beats allow, everywhere — evaluate deny → ask → allow → mode default.

The engine returns a *decision* (allow / ask / deny). The UI turns an ``ask``
into a modal via ``push_screen_wait``; headless turns it into an auto-deny.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

# Builtin read-only shell commands that auto-allow (first token).
READONLY_BUILTINS = {
    "ls", "cat", "pwd", "head", "tail", "wc", "which", "stat", "diff",
    "echo", "cd", "rg", "grep", "tree", "file", "basename", "dirname",
}
# Harmless wrappers stripped before matching (allow-side only).
WRAPPERS = {"timeout", "time", "nice", "nohup"}
# Splitters that break a command line into subcommands.
_SPLIT = re.compile(r"&&|\|\||\||;|&|\n")
# Substitution markers that forbid prefix-matching a rule.
_COMPOUND_MARKERS = ("$(", "`", ">", "<")
# Catastrophic patterns that prompt even in yolo.
_CIRCUIT_BREAKERS = [
    re.compile(r"\brm\s+-rf?\s+/(?:\s|$)"),
    re.compile(r"\brm\s+-rf?\s+~"),
    re.compile(r"git\s+push\s+.*--force"),
    re.compile(r":\(\)\s*\{"),  # fork bomb
]


class Mode(str, Enum):
    plan = "plan"
    ask = "ask"
    auto_edit = "auto-edit"
    dontask = "dontask"
    yolo = "yolo"


class Decision(str, Enum):
    allow = "allow"
    ask = "ask"
    deny = "deny"


@dataclass(frozen=True)
class PermissionSpec:
    """How a tool wants to be gated -- declared by the tool, not guessed here.

    The engine used to keep name lists of which tools mutate and which
    argument holds the path. That worked exactly as long as every tool was
    one we shipped: a plugin tool that wrote files got none of the protection
    ``write`` got, purely because it was not called ``write``. A tool now
    declares its own shape and the engine reads it.
    """

    # Changes something the user would want a say over: blocked in plan mode,
    # prompted in ask mode. Read-only tools are allowed by default.
    mutates: bool = True
    # Which Input field carries the thing being acted on, for rule matching
    # (``edit(src/**)``, ``bash(npm *)``).
    target_field: str | None = None
    # The target is a filesystem path: protected paths (.git, .env, .ssh,
    # anything outside the project) prompt before any allow rule applies.
    path_target: bool = False
    # The target is a shell command line and gets decomposed per subcommand.
    shell: bool = False


# What an unknown tool gets: treated as mutating, so a plugin that forgets to
# declare itself is prompted for rather than waved through.
DEFAULT_SPEC = PermissionSpec()

# Spawning or resuming a subagent doesn't touch the filesystem from the parent
# (the child's own actions are gated by its capped mode), and concurrent
# fan-out can't surface a separate modal per spawn. Cost stays visible in the
# status-bar meter.
READ_LIKE = PermissionSpec(mutates=False)


@dataclass
class Rules:
    allow: list[str] = field(default_factory=list)
    ask: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, root: Path) -> Rules:
        merged = cls()
        for rel in (".quickcode/settings.json", ".quickcode/settings.local.json"):
            p = root / rel
            if not p.exists():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8")).get("permissions", {})
            except Exception:
                continue
            merged.allow += data.get("allow", [])
            merged.ask += data.get("ask", [])
            merged.deny += data.get("deny", [])
        return merged

    def persist_allow(self, root: Path, rule: str) -> None:
        """Append a rule to settings.local.json (gitignored)."""
        d = root / ".quickcode"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "settings.local.json"
        data = {}
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        perms = data.setdefault("permissions", {})
        allow = perms.setdefault("allow", [])
        if rule not in allow:
            allow.append(rule)
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self.allow.append(rule)


def _rule_matches(rule: str, tool: str, arg: str) -> bool:
    """Match a rule like ``bash(npm *)`` / ``edit(src/**)`` / bare ``write``."""
    m = re.fullmatch(r"(\w+)\((.*)\)", rule)
    if not m:
        return rule.strip() == tool  # bare tool name
    rtool, pattern = m.group(1), m.group(2)
    if rtool != tool:
        return False
    return _glob_match(pattern, arg)


def _glob_match(pattern: str, value: str) -> bool:
    """Whole-string glob match where only ``**`` crosses directories."""
    parts: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append(r"[^/\\]*")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return re.fullmatch("".join(parts), value) is not None


def _protected(path: str, root: Path) -> bool:
    try:
        candidate = Path(path).expanduser()
        rp = (candidate if candidate.is_absolute() else root / candidate).resolve()
    except Exception:
        return True
    parts = set(rp.parts)
    if ".git" in parts or ".quickcode" in parts:
        return True
    # Secret-bearing files warrant an explicit prompt even inside the project.
    if any(part == ".ssh" or part == ".env" or part.startswith(".env.") for part in rp.parts):
        return True
    try:
        rp.relative_to(root.resolve())
    except ValueError:
        return True  # outside project root
    return False


_SPEC_CACHE: dict[str, PermissionSpec] | None = None


def registry_specs() -> dict[str, PermissionSpec]:
    """Permission shapes of the built-in tools, read off the tools themselves.

    Cached: the tool classes are static for the life of the process, and the
    engine asks for this on every call.
    """
    global _SPEC_CACHE
    if _SPEC_CACHE is None:
        try:
            from quickcode.tools.registry import default_registry

            _SPEC_CACHE = {
                name: getattr(tool, "permission", DEFAULT_SPEC)
                for name, tool in default_registry().tools.items()
            }
        except Exception:  # never let tool import trouble break the gate
            _SPEC_CACHE = {}
    return _SPEC_CACHE


@dataclass
class PermissionEngine:
    mode: Mode
    rules: Rules
    root: Path
    yolo_accepted: bool = False
    # Per-tool shapes for this session, including plugin and MCP tools. Empty
    # means "ask the built-in registry", which is what direct callers get.
    specs: dict[str, PermissionSpec] = field(default_factory=dict)

    def spec_for(self, tool: str) -> PermissionSpec:
        if tool in self.specs:
            return self.specs[tool]
        return registry_specs().get(tool, DEFAULT_SPEC)

    @staticmethod
    def target_for(spec: PermissionSpec, args: dict) -> str:
        """The argument a rule matches against, as the tool declares it."""
        if not spec.target_field:
            return ""
        value = args.get(spec.target_field)
        return "" if value is None else str(value)

    def evaluate_tool(self, tool, args: dict) -> tuple[Decision, str]:
        """Gate one call, given the tool object and its parsed arguments.

        Returns the decision and the target it was matched on, so the caller
        can show the user what they are approving.
        """
        spec = getattr(tool, "permission", DEFAULT_SPEC)
        target = self.target_for(spec, args)
        return self.evaluate(tool.name, target, spec=spec), target

    def evaluate(self, tool: str, arg: str, spec: PermissionSpec | None = None) -> Decision:
        """Decide for a single tool invocation. ``arg`` is the match target
        (a shell command line, or a path -- whichever the tool declares)."""
        spec = spec or self.spec_for(tool)
        is_write = spec.mutates
        is_read = not spec.mutates

        # 1. Protected paths always prompt (before any allow rule).
        if spec.path_target and _protected(arg, self.root):
            if self.mode in (Mode.dontask,):
                return Decision.deny
            return Decision.ask

        # 2. Shell tools get decomposed and evaluated per subcommand (handles
        #    plan mode itself — read-only builtins stay allowed, rest denied).
        if spec.shell:
            return self._eval_bash(arg)

        # 3. Plan mode structurally blocks file mutation.
        if self.mode == Mode.plan and is_write:
            return Decision.deny

        # 4. Rule evaluation: deny → ask → allow.
        for r in self.rules.deny:
            if _rule_matches(r, tool, arg):
                return Decision.deny
        for r in self.rules.ask:
            if _rule_matches(r, tool, arg):
                return Decision.ask
        for r in self.rules.allow:
            if _rule_matches(r, tool, arg):
                return Decision.allow

        # 5. Mode default.
        if is_read:
            return Decision.allow
        return self._mode_default_for_write()

    def _mode_default_for_write(self) -> Decision:
        if self.mode == Mode.yolo:
            return Decision.allow
        if self.mode == Mode.auto_edit:
            return Decision.allow  # edits auto; bash handled separately
        if self.mode == Mode.dontask:
            return Decision.deny
        return Decision.ask

    def _eval_bash(self, command: str) -> Decision:
        subs = [s.strip() for s in _SPLIT.split(command) if s.strip()]
        has_substitution = any(m in command for m in _COMPOUND_MARKERS)
        decisions: list[Decision] = []
        for sub in subs or [command]:
            decisions.append(self._eval_bash_sub(sub, command, has_substitution))
        # Circuit breakers apply to the whole line even in yolo.
        if any(cb.search(command) for cb in _CIRCUIT_BREAKERS):
            decisions.append(Decision.ask)
        # Most restrictive wins.
        if Decision.deny in decisions:
            return Decision.deny
        if Decision.ask in decisions:
            return Decision.ask
        return Decision.allow

    def _eval_bash_sub(self, sub: str, full: str, has_sub: bool) -> Decision:
        tokens = sub.split()
        # strip harmless wrappers and env-var prefixes for allow matching
        idx = 0
        while idx < len(tokens) and (
            tokens[idx] in WRAPPERS or re.fullmatch(r"\w+=.*", tokens[idx])
        ):
            idx += 1
        stripped = " ".join(tokens[idx:])
        first = tokens[idx].split("/")[-1] if idx < len(tokens) else ""

        # Shell reads must respect the same protected-path boundary as the
        # dedicated read tool. Treat every non-option argument as a potential
        # path; ordinary words resolve inside the project and remain harmless.
        for token in tokens[idx + 1 :]:
            if token.startswith("-"):
                continue
            candidate = token.split("=", 1)[-1] if "=" in token else token
            candidate = candidate.strip("'\"{},()")
            if candidate and _protected(candidate, self.root):
                return Decision.deny if self.mode == Mode.dontask else Decision.ask

        # deny rules first (against the substitution-free subcommand)
        for r in self.rules.deny:
            if _rule_matches(r, "bash", sub) or _rule_matches(r, "bash", stripped):
                return Decision.deny

        # builtin read-only → auto-allow (only when no substitution smuggling)
        if first in READONLY_BUILTINS and not has_sub:
            # plan mode allows read-only bash
            return Decision.allow

        if self.mode == Mode.plan:
            return Decision.deny  # only read-only builtins allowed in plan

        for r in self.rules.ask:
            if _rule_matches(r, "bash", sub):
                return Decision.ask
        # allow rules never prefix-match a compound/substitution line
        if not has_sub:
            for r in self.rules.allow:
                if _rule_matches(r, "bash", sub) or _rule_matches(r, "bash", stripped):
                    return Decision.allow

        if self.mode == Mode.yolo:
            return Decision.allow
        if self.mode == Mode.dontask:
            return Decision.deny
        return Decision.ask

    def suggest_rule(self, tool: str, arg: str) -> str:
        """The rule text an 'always allow' would persist."""
        if self.spec_for(tool).shell:
            first = arg.split()[0] if arg.split() else arg
            return f"bash({first} *)"
        return f"{tool}({arg})"


CYCLE = [Mode.plan, Mode.ask, Mode.auto_edit]


def next_mode(current: Mode, allow_yolo: bool) -> Mode:
    cycle = CYCLE + ([Mode.yolo] if allow_yolo else [])
    try:
        i = cycle.index(current)
    except ValueError:
        return Mode.ask
    return cycle[(i + 1) % len(cycle)]
