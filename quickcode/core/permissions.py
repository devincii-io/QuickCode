"""Permission engine: modes, rules, bash decomposition, plan-mode gate.

Two principles from docs/PERMISSIONS.md:
  1. Parse, don't prefix-match — decompose compound bash commands.
  2. Deny beats allow, everywhere — evaluate deny → ask → allow → mode default.

The engine returns a *decision* (allow / ask / deny). The UI turns an ``ask``
into a modal via ``push_screen_wait``; headless turns it into an auto-deny.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from quickcode.security import commands, shellwords
from quickcode.security.protected import (
    glob_may_name_protected,
    is_protected,
    is_protected_name,
    is_subagent_artifact,
    lexical_parts,
    resolve,
    written_tail,
)

log = logging.getLogger("quickcode.permissions")

# Builtin read-only shell commands that auto-allow (first token).
READONLY_BUILTINS = {
    "ls", "cat", "pwd", "head", "tail", "wc", "which", "stat", "diff",
    "echo", "cd", "rg", "grep", "tree", "file", "basename", "dirname",
}
# Harmless wrappers stripped before matching (allow-side only).
WRAPPERS = {"timeout", "time", "nice", "nohup"}
# ``NAME=value`` in front of a command. Commands run through ``bash -lc``, so
# the shell applies these to the environment the command executes in.
_ENV_ASSIGNMENT = re.compile(r"\w+=.*")
# Splitters that break a command line into subcommands.
_SPLIT = re.compile(r"&&|\|\||\||;|&|\n")
# Substitution markers that forbid prefix-matching a rule. An unquoted `(` is
# one too (``shellwords.has_unquoted_paren``): PowerShell runs `cat (rm x)`.
_COMPOUND_MARKERS = ("$(", "`", ">", "<")
# Commands run by other commands (`bash -c`, `xargs`, `find -exec`, `$(...)`)
# are evaluated as if typed, to this depth; anything nested deeper asks.
_MAX_NESTING = 4
# Whether deny and ask rules on paths ignore case: on these filesystems
# `KEY.PEM` opens `key.pem`, so a rule against one must hold for the other.
CASE_INSENSITIVE_PATHS = sys.platform in ("win32", "darwin")
# Catastrophic patterns that prompt even in yolo.
# The flag spelling was the whole of the check, so `rm -rf /` was caught while
# `rm -fr /`, `rm -rf /*` and `rm --recursive --force /` -- the same command,
# spelled the way a shell user is at least as likely to spell it -- went
# straight through, as did `git push -f`. Written now as "the dangerous flags,
# in any order or long form, then the dangerous target".
_RM_FLAG = r"(?:--recursive|--force|-[a-zA-Z]*[rRf][a-zA-Z]*)"
_CIRCUIT_BREAKERS = [
    re.compile(rf"\brm\s+(?:{_RM_FLAG}\s+)*{_RM_FLAG}\s+[\"']?(?:/|~)(?:/?\*)?[\"']?(?:\s|$)"),
    re.compile(r"git\s+push\s+(?:.*\s)?(?:--force\b|--force-with-lease\b|-f\b)"),
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
    def load(cls, root: Path, *, trusted: bool | None = None) -> Rules:
        """The project's rules, with its allowlist gated on project trust.

        ``deny`` and ``ask`` load from any project, trusted or not: they only
        ever narrow, and a project that can only narrow needs no consent.
        ``allow`` is the half that widens, so it loads only from a project the
        user has trusted once -- a cloned repository's committed allowlist is
        otherwise consent nobody gave. Both project settings files are gated:
        a repository can commit any filename it likes, so ``.local`` is a
        convention rather than a statement about where the file came from.

        The fallback is an empty allowlist, which is the state a project with
        no settings file is in -- so an untrusted project prompts, rather than
        failing to open. ``trusted`` is for tests; see ``trust.resolve_trust``.
        """
        from quickcode.security import trust

        allowed = trust.resolve_trust(root, trusted)
        merged = cls()
        refused = 0
        for rel in (".quickcode/settings.json", ".quickcode/settings.local.json"):
            p = root / rel
            if not p.exists():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8")).get("permissions", {})
            except Exception:
                continue
            if allowed:
                merged.allow += data.get("allow", [])
            else:
                refused += len(data.get("allow", []) or [])
            merged.ask += data.get("ask", [])
            merged.deny += data.get("deny", [])
        if refused:
            log.warning(
                "project %s is not trusted; %d permission allow rule(s) ignored",
                root, refused,
            )
        return merged

    def persist_allow(self, root: Path, rule: str) -> None:
        """Append a rule to settings.local.json (gitignored).

        The rule applies for the rest of this session either way. Whether it
        applies to the *next* one is the trust gate's answer, same as for every
        other allow rule -- ``load`` says why.
        """
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
        # This file is part of the project's trust hash, so writing to it used
        # to untrust the project -- and an untrusted project's allow rules are
        # ignored (see `load`). Answering "Always allow" therefore switched off
        # every allow rule the user had ever saved, and put the project's MCP
        # servers back behind the gate. A project that was not trusted stays
        # untrusted.
        from quickcode.security.trust import keep_trust

        with keep_trust(root):
            p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self.allow.append(rule)


# The tool-name half of a rule. Not `\w+`: an MCP tool is named
# ``mcp__<server>__<tool>`` and both halves come from outside -- a server called
# `company-kb` produces `mcp__company-kb__kb_search`, which `\w+` cannot spell.
# Rules naming one were read as a bare tool name nothing is called, so they
# matched nothing, forever, and the validator called them junk. Dots and colons
# are here for the same reason: servers name tools, we do not.
_RULE_TOOL = r"[\w.:-]+"


def _rule_matches(rule: str, tool: str, arg: str, *, fold: bool = False) -> bool:
    """Match a rule like ``bash(npm *)`` / ``edit(src/**)`` / bare ``write``."""
    m = re.fullmatch(rf"({_RULE_TOOL})\((.*)\)", rule)
    if not m:
        return rule.strip() == tool  # bare tool name
    rtool, pattern = m.group(1), m.group(2)
    if rtool != tool:
        return False
    return _glob_match(pattern, arg, fold=fold)


def _glob_match(pattern: str, value: str, *, fold: bool = False) -> bool:
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
    return re.fullmatch("".join(parts), value, re.IGNORECASE if fold else 0) is not None


def _path_rule_targets(arg: str, root: Path) -> tuple[list[str], list[str]]:
    """The strings a path rule is matched against: (for deny/ask, for allow).

    A path rule used to see only the string the tool was called with, so a
    deny on `src/secret.py` missed `./src/secret.py`, the absolute spelling
    and `lib/../src/secret.py` -- and an allow on `src/**` covered
    `src/../pyproject.toml`, which is not under `src` at all. Both kinds of
    rule now see where the path lands: root-relative and absolute.

    The spelling itself stays visible to deny and ask, which only narrow. It
    is offered to allow only when it names its location plainly -- no `..` and
    no symlink between the root and the file -- because that is the one case
    in which the spelling and the location are the same claim.
    """
    resolved = resolve(arg, root)
    if resolved is None:
        return [arg], []
    located = [resolved.as_posix(), str(resolved)]
    try:
        located.insert(0, resolved.relative_to(root.resolve()).as_posix())
    except ValueError:
        pass
    tail = written_tail(arg, root)
    plain = ".." not in lexical_parts(arg) and root.resolve().joinpath(*tail) == resolved
    restrict = list(dict.fromkeys([arg, *located]))
    allow = list(dict.fromkeys([*located, *([arg] if plain else [])]))
    return restrict, allow


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
        # A tool may know its effective location better than one field can say
        # (see Tool.permission_target). Its answer wins when it gives one.
        declared = getattr(tool, "permission_target", None)
        target = (declared(args) if callable(declared) else "") or self.target_for(spec, args)
        return self.evaluate(tool.name, target, spec=spec), target

    def evaluate(self, tool: str, arg: str, spec: PermissionSpec | None = None) -> Decision:
        """Decide for a single tool invocation. ``arg`` is the match target
        (a shell command line, or a path -- whichever the tool declares)."""
        spec = spec or self.spec_for(tool)

        # Shell tools get decomposed and evaluated per subcommand, through the
        # same order as below.
        if spec.shell:
            return self._eval_bash(arg)

        if spec.path_target:
            restrict, permit = _path_rule_targets(arg, self.root)
        else:
            restrict, permit = [arg], [arg]
        # Case is ignored where the filesystem ignores it, and only by the
        # rules that narrow: an allow rule keeps meaning exactly what it says.
        fold = spec.path_target and CASE_INSENSITIVE_PATHS

        def matches(rules: list[str], targets: list[str], ignore_case: bool = False) -> bool:
            return any(
                _rule_matches(r, tool, t, fold=ignore_case) for r in rules for t in targets
            )

        # 1. Deny rules first, before anything that could answer "ask". The
        #    protected-path prompt used to come first, so a `read(**.env)` deny
        #    was never consulted for `.env`: the user got a prompt, with an
        #    Allow button, for the one file they had said no to outright.
        if matches(self.rules.deny, restrict, fold):
            return Decision.deny

        # 2. Plan mode structurally blocks mutation -- ahead of the protected
        #    prompt too, or a write to `.git/config` in plan mode was a prompt
        #    the user could click through rather than a refusal.
        if self.mode == Mode.plan and spec.mutates:
            return Decision.deny

        # 3. Protected paths prompt before any allow rule -- except in yolo,
        #    which is the mode whose entire promise is that it does not ask.
        #    Prompting there was the rule outliving its reason: it exists so an
        #    ordinary session cannot wander into `.git`, `.env` or the world
        #    outside the project without a word, and somebody who has turned on
        #    the mode named yolo, confirmed it, and watched it go red has
        #    already had that conversation.
        if spec.path_target and is_protected(arg, self.root):
            if spec.mutates or not is_subagent_artifact(arg, self.root):
                if self.mode is Mode.dontask:
                    return Decision.deny
                if self.mode is not Mode.yolo:
                    return Decision.ask

        # 4. The rest of the rules: ask, then allow.
        if matches(self.rules.ask, restrict, fold):
            return Decision.ask
        if matches(self.rules.allow, permit):
            return Decision.allow

        # 5. Mode default.
        if not spec.mutates:
            return Decision.allow
        return self._mode_default_for_write(spec)

    def _mode_default_for_write(self, spec: PermissionSpec) -> Decision:
        if self.mode == Mode.yolo:
            return Decision.allow
        # auto-edit auto-allows *edits*: a tool whose target is a path, which
        # the protected check above has already confined to the project. It
        # used to allow every mutating tool, so `web_fetch` (a way out for any
        # file the agent has read), a plugin's command tool and every MCP tool
        # that writes ran unprompted in the mode documented as "edits only".
        if self.mode == Mode.auto_edit and spec.path_target:
            return Decision.allow
        if self.mode == Mode.dontask:
            return Decision.deny
        return Decision.ask

    def _eval_bash(self, command: str, depth: int = 0) -> Decision:
        subs = [s.strip() for s in _SPLIT.split(command) if s.strip()]
        has_substitution = any(
            m in command for m in _COMPOUND_MARKERS
        ) or shellwords.has_unquoted_paren(command)
        decisions: list[Decision] = []
        for sub in subs or [command]:
            decisions.append(self._eval_bash_sub(sub, has_substitution))
        # A command another command runs is decided as if it had been typed:
        # `find . -exec rm {} +`, `xargs rm`, `sudo rm`, `bash -c 'rm ...'`,
        # `echo $(rm ...)` and `git -c alias.x='!rm ...' x` all run `rm`. A deny
        # rule on `rm` must see it, and an allow rule on `find` must not cover
        # it -- the most restrictive answer below makes both true.
        inner = commands.inner_lines(command)
        if inner and depth >= _MAX_NESTING:
            decisions.append(Decision.ask)
        elif inner:
            decisions += [self._eval_bash(line, depth + 1) for line in inner]
        # Circuit breakers apply to the whole line even in yolo.
        if any(cb.search(command) for cb in _CIRCUIT_BREAKERS):
            decisions.append(Decision.ask)
        # Most restrictive wins.
        if Decision.deny in decisions:
            return Decision.deny
        if Decision.ask in decisions:
            return Decision.ask
        return Decision.allow

    def _eval_bash_sub(self, sub: str, has_sub: bool) -> Decision:
        tokens = sub.split()
        lexed = shellwords.segments(sub)
        analysis = commands.analyze(lexed[0], base=self.root) if lexed else commands.Analysis()
        # Strip harmless wrappers and env-var prefixes so a rule written against
        # the command still matches. Whether an assignment was among them is
        # remembered, because the two kinds of prefix are not equally harmless.
        idx = 0
        has_env_prefix = False
        while idx < len(tokens) and (
            tokens[idx] in WRAPPERS or _ENV_ASSIGNMENT.fullmatch(tokens[idx])
        ):
            has_env_prefix = has_env_prefix or bool(_ENV_ASSIGNMENT.fullmatch(tokens[idx]))
            idx += 1
        stripped = " ".join(tokens[idx:])
        # The command word as the shell reads it: `r''m`, `"rm"` and `\\rm` are
        # all `rm`, and a deny rule on `rm` has to see that.
        spelled = shellwords.dequote(tokens[idx]) if idx < len(tokens) else ""
        first = re.split(r"[\\/]", spelled)[-1]
        # The command as its bare name: `/bin/cat x` -> `cat x`. The auto-allow
        # for read-only builtins already worked on the basename, so `/bin/cat`
        # was auto-allowed -- while a deny rule was matched against the full
        # string and `bash(cat **)` did not cover it. Writing the absolute path
        # therefore walked straight through the rule that forbade the command,
        # which is how the built-in "Survey" posture stopped holding. Rules that
        # *restrict* (deny, ask) are matched against this form too.
        by_name = " ".join([first, *tokens[idx + 1 :]]) if idx < len(tokens) else stripped

        # 1. Deny rules first (against the substitution-free subcommand), for
        #    the same reason as in ``evaluate``: a protected path must not turn
        #    a deny into a prompt.
        for r in self.rules.deny:
            if (
                _rule_matches(r, "bash", sub)
                or _rule_matches(r, "bash", stripped)
                or _rule_matches(r, "bash", by_name)
            ):
                return Decision.deny

        # Builtin read-only commands auto-allow -- only when there is no
        # substitution smuggling and no rewritten environment.
        #
        # Every assignment disqualifies, not a blocklist of the dangerous
        # names. A blocklist here would have to be complete, and it cannot be:
        # `PATH` and `LD_PRELOAD` are only the obvious entries next to
        # `BASH_ENV`, `IFS`, `GLOBIGNORE`, `PYTHONSTARTUP`, `NODE_OPTIONS`,
        # `LESSOPEN` -- and `RIPGREP_CONFIG_PATH`, which points `rg` (a
        # read-only builtin, right here in the list) at a config file that may
        # set `--pre`, which runs a program. The set of variables that turn a
        # harmless command into an arbitrary one grows with every program
        # installed on the machine, so it is not knowable from here.
        #
        # The conservative reading costs one prompt for `FOO=1 ls`, which is
        # not a command anybody types by hand, and the auto-allow exists to
        # make the ordinary case frictionless rather than to cover every case.
        # A builtin can still run or write something through an option
        # (`rg --pre`, `tree -o`, `file -C`); ``commands.analyze`` knows which.
        read_only = (
            first in READONLY_BUILTINS
            and self._runs_from_path(spelled)
            and not has_sub
            and not has_env_prefix
            and not analysis.unsafe_read_only
        )

        # 2. Plan mode runs the read-only builtins and nothing else -- decided
        #    before the protected-path prompt, which would otherwise offer to
        #    run `rm .git/index` in plan mode rather than refuse it.
        if self.mode == Mode.plan and not read_only:
            return Decision.deny

        # 3. Shell reads respect the same protected-path boundary as the
        #    dedicated read tool. Every argument is a potential path; ordinary
        #    words resolve inside the project and stay harmless.
        # `git config` writes `.git/config`, the file every later git command
        # takes its pager, editor and hooks path from.
        if analysis.writes_protected or self._names_protected(tokens[idx:], sub):
            if self.mode is Mode.dontask:
                return Decision.deny
            # Same exemption as the path tools, and this is where it was felt:
            # every argument is treated as a possible path, so `find / -name
            # "*x*"` prompted in yolo because of the `/`. A mode that promises
            # not to ask must not ask here.
            if self.mode is not Mode.yolo:
                return Decision.ask

        # 4. Read-only builtins run without a prompt, in every mode.
        if read_only:
            return Decision.allow

        for r in self.rules.ask:
            if _rule_matches(r, "bash", sub) or _rule_matches(r, "bash", by_name):
                return Decision.ask
        # Allow rules never prefix-match a compound/substitution line, and the
        # env-stripped form is not offered to them either: approving
        # `git status` is not approving `LD_PRELOAD=./x.so git status`. A rule
        # that spells the assignment out still matches, via ``sub``. Nor do
        # they cover a command that points its program at code the rule never
        # saw (`git -c core.pager=...`, `git --exec-path=...`).
        if not has_sub and not analysis.opaque:
            for r in self.rules.allow:
                if _rule_matches(r, "bash", sub) or (
                    not has_env_prefix and _rule_matches(r, "bash", stripped)
                ):
                    return Decision.allow

        if self.mode == Mode.yolo:
            return Decision.allow
        if self.mode == Mode.dontask:
            return Decision.deny
        return Decision.ask

    def _runs_from_path(self, command: str) -> bool:
        """Whether a command word names a program found on PATH (or installed
        outside the project), rather than a file inside it.

        The read-only auto-allow is for `cat` the system program. It was
        matched on the basename, so `./cat`, `bin/ls` or `tools/grep` -- a file
        the repository ships, with whatever it contains -- ran unprompted in
        every mode, plan included.
        """
        if not re.search(r"[\\/]", command):
            return True
        resolved = resolve(command, self.root)
        if resolved is None or not Path(command).expanduser().is_absolute():
            return False
        return not resolved.is_relative_to(self.root.resolve())

    def _names_protected(self, words: list[str], sub: str) -> bool:
        """Whether any word of a subcommand may name a protected path.

        Each word is read every way the shell might read it
        (``shellwords.path_candidates``): quotes and escapes removed, braces
        expanded, option values and assignment right-hand sides split off. A
        glob is protected if it could expand to a protected name. The words
        are also taken from a quote-aware split, so a redirection glued to
        its target (`cat<.env`) is seen.
        """
        if not words:
            return False
        # The command word is where a program comes from, not what it reads:
        # every program on PATH lives outside the project. Only a protected
        # *name* counts there (`.git/hooks/post-checkout`).
        for candidate in shellwords.path_candidates(words[0]) or []:
            if any(is_protected_name(p) for p in lexical_parts(candidate)):
                return True
        lexed = [w for segment in shellwords.segments(sub) for w in segment[1:]]
        for word in [*words[1:], *lexed]:
            candidates = shellwords.path_candidates(word)
            if candidates is None:
                return True
            if any(self._candidate_protected(c) for c in candidates):
                return True
        return False

    def _candidate_protected(self, candidate: str) -> bool:
        if is_protected(candidate, self.root):
            return True
        return shellwords.has_glob(candidate) and any(
            glob_may_name_protected(p, dotfiles=shellwords.GLOB_MATCHES_DOTFILES)
            for p in lexical_parts(candidate)
            if shellwords.has_glob(p)
        )

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
