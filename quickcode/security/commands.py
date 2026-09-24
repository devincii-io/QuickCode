"""What a command line does beyond the command it names.

The engine's first question is "what is this command?", and the answer it got
from the first word was often the wrong one:

- **it runs another program.** ``find . -exec rm {} +``, ``xargs rm``,
  ``env rm``, ``sudo rm``, ``bash -c 'rm ...'``, ``eval rm ...``,
  ``git -c alias.x='!rm ...' x`` and ``rg --pre rm`` all run ``rm``. A deny
  rule on ``rm`` has to see it, and an allow rule on ``find`` must not cover
  it. ``inner_lines`` returns every such command so the engine can evaluate
  each one as if it had been typed.
- **it writes.** ``tree -o FILE`` and ``file -C`` write files; ``rg --pre``
  and ``rg --hostname-bin`` run programs. Each is on the read-only list, and
  each lost its auto-allow here (``Analysis.unsafe_read_only``).
- **it rewrites git's own configuration** (``git config core.pager ...``),
  which is how a later, innocent ``git log`` runs a program.
  (``Analysis.writes_protected``)
- **it hides what git will run.** ``git -c <key>=<value>`` sets any of the
  dozens of configuration keys that name a program; ``--exec-path``,
  ``--git-dir`` and ``--work-tree`` point git at code or configuration that is
  not the project's own. No allow rule written against ``git`` covers them.
  (``Analysis.opaque``)
- **it reads a whole tree.** ``grep -r``, ``rg --hidden``, ``rg -g '*'`` and
  ``diff -r`` read every file under a directory, ``.env`` and ``.git``
  included. (``Analysis.sweep``, checked against the disk by ``sweep.py``)
"""

from __future__ import annotations

import base64
import binascii
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from quickcode.security.shellwords import segments, substitutions

_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "mksh", "ash", "fish"})
_SHELL_OPTS_WITH_ARG = frozenset({"-o", "+o", "-O", "+O", "--rcfile", "--init-file"})
_KEYWORDS = frozenset({"if", "then", "elif", "else", "do", "while", "until", "!", "{"})


@dataclass(frozen=True)
class _Wrapper:
    """A command whose job is to run the command after it."""

    with_arg: frozenset[str] = frozenset()
    positionals: int = 0          # operands of its own before the command
    string: bool = False          # the rest is one shell string, not an argv
    command_opts: frozenset[str] = frozenset()  # an option whose value is a shell string


def _w(with_arg: str = "", positionals: int = 0, string: bool = False,
       command_opts: str = "") -> _Wrapper:
    return _Wrapper(frozenset(with_arg.split()), positionals, string,
                    frozenset(command_opts.split()))


_WRAPPERS: dict[str, _Wrapper] = {
    "exec": _w("-a"),
    "command": _w(),
    "builtin": _w(),
    "nohup": _w(),
    "time": _w(),
    "unbuffer": _w(),
    "caffeinate": _w("-w -t"),
    "dbus-run-session": _w(),
    "setsid": _w(),
    "busybox": _w(),
    "toybox": _w(),
    "env": _w("-u --unset -C --chdir", command_opts="-S --split-string"),
    "sudo": _w("-u -g -C -D -h -p -r -t -T -U --user --group --host --prompt "
               "--role --type --chdir --close-from --command-timeout --other-user"),
    "doas": _w("-u -C"),
    "nice": _w("-n --adjustment"),
    "ionice": _w("-c -n -p -t --class --classdata"),
    "timeout": _w("-s --signal -k --kill-after", positionals=1),
    "stdbuf": _w("-i -o -e --input --output --error"),
    "chroot": _w("--userspec --groups", positionals=1),
    "chrt": _w(positionals=1),
    "taskset": _w(positionals=1),
    "strace": _w("-o -e -p -s -u -E -a -b -I -O -S -P -X"),
    "ltrace": _w("-o -e -p -s -u -E -a -n -S"),
    "xargs": _w("-a -d -E -I -L -n -P -s --arg-file --delimiter --eof --max-lines "
                "--max-args --max-procs --max-chars --process-slot-var"),
    "watch": _w("-n --interval -g --chgexit -e --errexit -x --exec", string=True),
    "flock": _w("-w --timeout -E --conflict-exit-code", positionals=1,
                command_opts="-c --command"),
    "script": _w("-E -I -O -B -T -t", positionals=1, command_opts="-c --command"),
    "su": _w("-s -g -G --shell --group --supp-group", positionals=1,
             command_opts="-c --command"),
    "runuser": _w("-u -s -g -G --user --shell --group --supp-group", positionals=1,
                  command_opts="-c --command"),
}


@dataclass
class Analysis:
    """What the engine needs to know about one simple command."""

    inner: list[str] = field(default_factory=list)
    opaque: bool = False
    unsafe_read_only: bool = False
    writes_protected: bool = False
    sweep: Sweep | None = None


@dataclass(frozen=True)
class Sweep:
    """A recursive read: which directories, and what inside them gets read."""

    roots: tuple[str, ...]
    hidden: bool
    globs: tuple[str, ...] = ()
    follow: bool = False


def command_name(word: str) -> str:
    """``/usr/bin/rm``, ``RM.EXE`` and ``rm`` are all ``rm``."""
    name = re.split(r"[\\/]", word)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _short_cluster(word: str) -> str:
    """The letters of a single-dash option cluster (``-rn`` -> ``rn``), else ""."""
    return word[1:] if len(word) > 1 and word[0] == "-" and word[1] != "-" else ""


def _long(word: str, *names: str) -> str | None:
    """The value of ``--name=value``, "" for a bare ``--name``, None otherwise."""
    for name in names:
        if word == name:
            return ""
        if word.startswith(name + "="):
            return word[len(name) + 1:]
    return None


def _skip_assignments(words: list[str]) -> list[str]:
    i = 0
    while i < len(words) and re.fullmatch(r"[A-Za-z_]\w*=.*", words[i]):
        i += 1
    return words[i:]


def _unwrap(spec: _Wrapper, args: list[str]) -> list[str]:
    """Inner command lines of a wrapper invocation."""
    i = 0
    positionals = spec.positionals
    while i < len(args):
        word = args[i]
        if word == "--":
            i += 1
            break
        if word in spec.command_opts:
            return [args[i + 1]] if i + 1 < len(args) else []
        if word.split("=", 1)[0] in spec.command_opts and "=" in word:
            return [word.split("=", 1)[1]]
        if word.startswith("-") and len(word) > 1:
            i += 2 if word in spec.with_arg else 1
            continue
        if re.fullmatch(r"[A-Za-z_]\w*=.*", word):
            i += 1
            continue
        if positionals:
            positionals -= 1
            i += 1
            continue
        break
    rest = args[i:]
    if not rest:
        return []
    return [" ".join(rest) if spec.string else shlex.join(rest)]


def _shell_string(args: list[str]) -> list[str]:
    """``bash -lc 'cmd'`` -> ``cmd``: the first operand after a ``c`` option."""
    has_c = False
    i = 0
    while i < len(args):
        word = args[i]
        if word in _SHELL_OPTS_WITH_ARG:
            i += 2
            continue
        if word.startswith(("-", "+")) and len(word) > 1 and word != "--":
            if not word.startswith("--") and "c" in word[1:]:
                has_c = True
            i += 1
            continue
        if word == "--":
            i += 1
        break
    return [args[i]] if has_c and i < len(args) else []


def _find_exec(args: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(args):
        if args[i] in ("-exec", "-execdir", "-ok", "-okdir"):
            j = i + 1
            while j < len(args) and args[j] not in (";", "+"):
                j += 1
            if j > i + 1:
                out.append(shlex.join(args[i + 1:j]))
            i = j
        i += 1
    return out


def _powershell(args: list[str]) -> list[str]:
    for i, word in enumerate(args):
        if not word.startswith(("-", "/")):
            return [" ".join(args[i:])]
        flag = word[1:].lower()
        if flag and "command".startswith(flag) and flag[0] == "c":
            return [" ".join(args[i + 1:])] if i + 1 < len(args) else []
        if flag in ("e", "ec") or (len(flag) > 1 and "encodedcommand".startswith(flag)):
            if i + 1 >= len(args):
                return []
            try:
                return [base64.b64decode(args[i + 1]).decode("utf-16-le")]
            except (binascii.Error, ValueError):
                return ["$(undecodable)"]
        if flag and "file".startswith(flag) and flag[0] == "f":
            return []
    return []


def _cmd(args: list[str]) -> list[str]:
    for i, word in enumerate(args):
        if word.lower() in ("/c", "/k", "/r"):
            return [" ".join(args[i + 1:]).replace("^", "")]
    return []


def inner_lines(line: str) -> list[str]:
    """Every command line ``line`` runs besides its own simple commands."""
    out = substitutions(line)
    for words in segments(line):
        out += analyze(words).inner
    return [s for s in out if s.strip()]


def analyze(words: list[str], *, base: Path | None = None) -> Analysis:
    """Read one simple command (dequoted words). ``base`` is the directory
    relative paths are relative to, for checks that look at the disk."""
    words = _skip_assignments(words)
    if not words:
        return Analysis()
    name = command_name(words[0])
    args = words[1:]
    result = Analysis()
    if name in _KEYWORDS:
        result.inner = [shlex.join(args)] if args else []
    elif name in _SHELLS:
        result.inner = _shell_string(args)
    elif name == "eval":
        result.inner = [" ".join(args)] if args else []
    elif name == "cmd":
        result.inner = _cmd(args)
    elif name in ("powershell", "pwsh"):
        result.inner = _powershell(args)
    elif name in ("iex", "invoke-expression"):
        result.inner = [" ".join(args)] if args else []
    elif name == "find":
        result.inner = _find_exec(args)
    elif name in _WRAPPERS:
        result.inner = _unwrap(_WRAPPERS[name], args)
    elif name == "git":
        _git(args, result, base)
    elif name == "rg":
        _ripgrep(args, result)
    elif name == "grep":
        _grep(args, result)
    elif name == "diff":
        if any(a == "--recursive" or "r" in _short_cluster(a) for a in args):
            roots = tuple(a for a in args if not a.startswith("-"))
            result.sweep = Sweep(roots=roots, hidden=True, follow=True)
    elif name == "tree":
        result.unsafe_read_only = any(
            set(_short_cluster(a)) & {"o", "R"} for a in args
        )
    elif name == "file":
        result.unsafe_read_only = any(
            a == "--compile" or "C" in _short_cluster(a) for a in args
        )
    return result


# ------------------------------------------------------------------ ripgrep

def _ripgrep(args: list[str], result: Analysis) -> None:
    hidden = follow = False
    unrestricted = 0
    globs: list[str] = []
    operands: list[str] = []
    i = 0
    while i < len(args):
        word = args[i]
        for option in ("--pre", "--hostname-bin"):
            value = _long(word, option)
            if value is not None:
                result.unsafe_read_only = True
                if value == "" and i + 1 < len(args):
                    value = args[i + 1]
                    i += 1
                if value:
                    # `--pre` runs its program once per file, with the path.
                    argv = [value, "{}"] if option == "--pre" else [value]
                    result.inner.append(shlex.join(argv))
        glob = _long(word, "--glob", "--iglob")
        if glob is None and word.startswith("-g") and not word.startswith("--"):
            glob = word[2:]
        if glob is not None:
            if glob == "" and i + 1 < len(args):
                glob = args[i + 1]
                i += 1
            if glob and not glob.startswith("!"):
                globs.append(glob)
        elif word in ("--hidden", "-."):
            hidden = True
        elif word == "--unrestricted":
            unrestricted += 1
        elif word == "--follow":
            follow = True
        elif word in ("-e", "--regexp", "-f", "--file", "-t", "--type", "-T", "--type-not",
                      "-m", "--max-count", "-A", "-B", "-C", "-j", "--threads", "-M"):
            i += 1
        elif _short_cluster(word):
            cluster = _short_cluster(word)
            unrestricted += cluster.count("u")
            hidden = hidden or "." in cluster
            follow = follow or "L" in cluster
        elif not word.startswith("-"):
            operands.append(word)
        i += 1
    hidden = hidden or unrestricted >= 2
    if hidden or follow or globs:
        # The first operand is the pattern unless -e/-f supplied one; keeping
        # it costs nothing, since only operands that are directories count.
        result.sweep = Sweep(tuple(operands), hidden=hidden, globs=tuple(globs), follow=follow)


def _grep(args: list[str], result: Analysis) -> None:
    recursive = follow = False
    operands: list[str] = []
    i = 0
    while i < len(args):
        word = args[i]
        cluster = _short_cluster(word)
        if word in ("--recursive", "--directories=recurse") or (cluster and "r" in cluster):
            recursive = True
        if word == "--dereference-recursive" or (cluster and "R" in cluster):
            recursive = follow = True
        if word in ("-d", "--directories") and i + 1 < len(args):
            recursive = recursive or args[i + 1] == "recurse"
            i += 1
        elif word in ("-e", "--regexp", "-f", "--file", "-m", "--max-count", "-A", "-B", "-C",
                      "-D", "--devices", "--include", "--exclude", "--exclude-dir",
                      "--label", "--group-separator"):
            i += 1
        elif not word.startswith("-"):
            operands.append(word)
        i += 1
    if recursive:
        result.sweep = Sweep(tuple(operands), hidden=True, follow=follow)


# ---------------------------------------------------------------------- git

_GIT_GLOBAL_WITH_ARG = frozenset({
    "-c", "-C", "--git-dir", "--work-tree", "--namespace", "--config-env",
    "--exec-path", "--super-prefix", "--list-cmds", "--attr-source",
})
# Global options that point git at code or configuration the project does not
# own. `-C` is judged by where it points (see `_git`).
_GIT_OPAQUE_GLOBAL = frozenset({"-c", "--config-env", "--exec-path", "--git-dir", "--work-tree"})
# Configuration keys whose value is a program git runs.
_GIT_EXEC_KEY = re.compile(
    r"(?:core\.(?:pager|editor|sshcommand|fsmonitor|askpass|gitproxy)|sequence\.editor"
    r"|diff\.external|gpg(?:\.\w+)?\.program|credential(?:\..+)?\.helper"
    r"|.+\.(?:command|cmd|textconv|clean|smudge|process|driver|tool))",
    re.I,
)
_GIT_CONFIG_READS = frozenset({
    "--get", "--get-all", "--get-regexp", "--get-urlmatch", "--get-color",
    "--get-colorbool", "-l", "--list", "get", "list",
})
_GIT_CONFIG_WRITES = frozenset({
    "set", "unset", "rename-section", "remove-section", "edit", "--add", "--unset",
    "--unset-all", "--replace-all", "--rename-section", "--remove-section", "-e", "--edit",
})
# Subcommand options whose value is a command git runs: (subcommands, options).
_GIT_EXEC_OPTS: tuple[tuple[frozenset[str], frozenset[str]], ...] = (
    (frozenset({"rebase"}), frozenset({"-x", "--exec"})),
    (frozenset({"difftool"}), frozenset({"-x", "--extcmd"})),
    (frozenset({"fetch", "pull", "clone", "ls-remote", "archive"}), frozenset({"--upload-pack"})),
    (frozenset({"clone", "ls-remote"}), frozenset({"-u"})),
    (frozenset({"push", "send-pack"}), frozenset({"--receive-pack", "--exec"})),
    (frozenset({"archive"}), frozenset({"--exec"})),
    (frozenset({"grep"}), frozenset({"-O", "--open-files-in-pager"})),
    (frozenset({"filter-branch"}), frozenset({
        "--env-filter", "--tree-filter", "--index-filter", "--parent-filter",
        "--msg-filter", "--commit-filter", "--tag-name-filter",
    })),
)


def git_invocation(args: list[str]) -> tuple[list[str], dict[str, str], list[str], list[str]]:
    """Split git's arguments: (global options, -c settings, subcommand + args,
    -C targets). Aliases set with ``-c alias.x=...`` are expanded once."""
    globals_: list[str] = []
    settings: dict[str, str] = {}
    chdirs: list[str] = []
    i = 0
    while i < len(args) and args[i].startswith("-"):
        word = args[i]
        globals_.append(word.split("=", 1)[0])
        value = None
        if word in _GIT_GLOBAL_WITH_ARG and i + 1 < len(args):
            value = args[i + 1]
            i += 1
        elif "=" in word:
            value = word.split("=", 1)[1]
        if word == "-c" and value is not None and "=" in value:
            key, val = value.split("=", 1)
            settings[key.lower()] = val
        if word == "-C" and value is not None:
            chdirs.append(value)
        i += 1
    rest = args[i:]
    if rest:
        alias = settings.get(f"alias.{rest[0].lower()}")
        if alias is not None and not alias.startswith("!"):
            expanded = segments(alias)
            rest = (expanded[0] if expanded else []) + rest[1:]
    return globals_, settings, rest, chdirs


def _looks_like_git_dir(path: str, base: Path | None) -> bool:
    candidate = Path(path).expanduser()
    if base is not None and not candidate.is_absolute():
        candidate = base / candidate
    try:
        return (candidate / "HEAD").is_file() or (candidate / "config").is_file()
    except OSError:
        return True


def _git(args: list[str], result: Analysis, base: Path | None) -> None:
    globals_, settings, rest, chdirs = git_invocation(args)
    if any(g in _GIT_OPAQUE_GLOBAL for g in globals_):
        result.opaque = True
    # A bare repository committed inside a project carries its own `config`,
    # and `git -C` into it runs whatever that config names as a pager.
    if any(_looks_like_git_dir(d, base) for d in chdirs):
        result.opaque = True
    for key, value in settings.items():
        if key.startswith("alias.") and value.startswith("!"):
            result.inner.append(value[1:])
        elif _GIT_EXEC_KEY.fullmatch(key) and value:
            result.inner.append(value[1:] if value.startswith("!") else value)
    if not rest:
        return
    sub, sub_args = rest[0].lower(), rest[1:]
    if sub == "config":
        positional = [a for a in sub_args if not a.startswith("-")]
        reads = any(a in _GIT_CONFIG_READS for a in sub_args)
        writes = any(a in _GIT_CONFIG_WRITES for a in sub_args)
        result.writes_protected = writes or (not reads and len(positional) >= 2)
    elif sub in ("clone", "init") and any(
        a in ("-c", "--config") or _long(a, "--template", "--config") is not None
        for a in sub_args
    ):
        result.opaque = True
    elif sub == "bisect" and sub_args[:1] == ["run"] and len(sub_args) > 1:
        result.inner.append(shlex.join(sub_args[1:]))
    elif sub == "submodule" and "foreach" in sub_args:
        tail = sub_args[sub_args.index("foreach") + 1:]
        tail = [a for a in tail if a != "--recursive"]
        if tail:
            result.inner.append(" ".join(tail))
    for subs, options in _GIT_EXEC_OPTS:
        if sub not in subs:
            continue
        for i, word in enumerate(sub_args):
            for option in options:
                value = _long(word, option) if option.startswith("--") else (
                    word[len(option):] if word.startswith(option) else None
                )
                if value is None:
                    continue
                if value == "" and i + 1 < len(sub_args):
                    value = sub_args[i + 1]
                result.opaque = True
                if value:
                    result.inner.append(value)
