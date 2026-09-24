"""Circuit breakers: the few commands that prompt even in yolo.

Recursive deletion of the filesystem root or a home directory, a forced git
push, and a fork bomb. Matched against the words of the command, not against
one spelling of it: ``rm -rf --no-preserve-root /``, ``rm -rf build /``,
``rm -rf "$HOME"``, ``git -C . push -f``, ``git push origin +main`` and a fork
bomb whose function is not called ``:`` are the same commands as the
spellings the regexes used to know. Nested command lines (``bash -c``, ``$()``,
``xargs``...) reach this through the engine's evaluation of inner commands.
"""

from __future__ import annotations

import re
from pathlib import Path

from quickcode.security.commands import command_name, git_invocation
from quickcode.security.shellwords import segments

_DELETERS = frozenset({"rm", "remove-item", "ri", "rmdir", "rd", "del", "erase"})
_TOP_LEVEL = frozenset({
    "bin", "boot", "dev", "etc", "home", "lib", "lib32", "lib64", "libx32", "opt", "proc",
    "root", "run", "sbin", "srv", "sys", "usr", "var", "users", "system", "library",
    "applications", "windows", "program files", "program files (x86)", "programdata",
})
_HOME_WORD = re.compile(
    r"~[\w.-]*"
    r"|\$\{?(?:home|env:userprofile|env:home|env:homepath)(?:[:?=+-][^}]*)?\}?"
    r"|%(?:userprofile|systemdrive|homedrive%%homepath|systemroot|windir)%"
    r"|\$\{?env:(?:systemdrive|systemroot|windir)\}?",
    re.I,
)
# A function definition's opening -- `name() {` or `function name {` -- found
# first and its name read backwards, rather than one regex that starts a name
# at every character: that form was quadratic, and a 100 KB word took minutes.
_FUNCTION_OPEN = re.compile(r"\(\s*\)\s*\{|\bfunction\s+([\w:.-]+)\s*(?:\(\s*\))?\s*\{")
_OTHER_BOMBS = re.compile(r"\bfork\s*(?:\(\s*\))?\s*while\s+fork\b|%0\s*\|\s*%0")
# A fork bomb's body is short; a longer one is read only this far.
_BODY_LIMIT = 4096


def _is_fork_bomb(line: str) -> bool:
    """A function that pipes itself into itself in the background, whatever it
    is called: `:(){ :|:& };:`, `f(){ f|f& };f`, `function b { b|b& }`."""
    if _OTHER_BOMBS.search(line):
        return True
    for m in _FUNCTION_OPEN.finditer(line):
        name = m.group(1)
        if name is None:
            end = m.start()
            while end > 0 and line[end - 1].isspace():
                end -= 1
            start = end
            while start > 0 and (line[start - 1].isalnum() or line[start - 1] in "_:.-"):
                start -= 1
            name = line[start:end]
        if not name:
            continue
        body = line[m.end():m.end() + _BODY_LIMIT].split("}", 1)[0]
        n = re.escape(name)
        if re.search(rf"(?<![\w:.-]){n}\s*\|\s*&?\s*{n}(?![\w:.-])", body):
            return True
    return False


def _is_root_or_home(word: str) -> bool:
    w = re.sub(r"/+", "/", word.replace("\\", "/"))
    while len(w) > 1 and w.endswith(("/", "/*", "/.", "/.*")):
        w = w[:-1] if w.endswith("/") else w.rsplit("/", 1)[0] or "/"
    if w == "/" or _HOME_WORD.fullmatch(w):
        return True
    if re.fullmatch(r"[a-zA-Z]:|/[a-zA-Z]|/(?:mnt|cygdrive)/[a-zA-Z]", w):
        return True
    top = re.fullmatch(r"(?:[a-zA-Z]:)?/([^/]+)", w)
    if top and top.group(1).lower() in _TOP_LEVEL:
        return True
    home = str(Path.home()).replace("\\", "/").rstrip("/").lower()
    return w.lower() == home


def _is_destructive_flag(word: str) -> bool:
    """``-r``/``-f`` in any cluster, their GNU long forms, PowerShell's
    ``-Recurse``/``-Force`` (any prefix), and cmd's ``/s``/``/q``."""
    w = word.lower()
    if w.startswith("--"):
        return w in ("--recursive", "--force", "--no-preserve-root")
    if w.startswith("/"):
        return w in ("/s", "/q")
    flag = w[1:]
    if flag and ("recurse".startswith(flag) or "force".startswith(flag)):
        return True
    return flag.isalpha() and len(flag) <= 6 and bool(set(flag) & {"r", "f"})


def _deletes_root_or_home(words: list[str]) -> bool:
    for i, word in enumerate(words):
        name = command_name(word)
        if name not in _DELETERS:
            continue
        cmd_style = name in ("rmdir", "rd", "del", "erase")
        flags: list[str] = []
        operands: list[str] = []
        after_options = False
        for arg in words[i + 1:]:
            if after_options:
                operands.append(arg)
            elif arg == "--":
                after_options = True
            elif (arg.startswith("-") and len(arg) > 1) or (
                cmd_style and arg.lower() in ("/s", "/q")
            ):
                flags.append(arg)
            else:
                operands.append(arg)
        if any(_is_destructive_flag(f) for f in flags) and any(
            _is_root_or_home(o) for o in operands
        ):
            return True
    return False


def _forces_push(args: list[str], depth: int = 0) -> bool:
    _, settings, rest, _ = git_invocation(args)
    for key, value in settings.items():
        if key.startswith("alias.") and value.startswith("!") and depth < 3:
            if tripped(value[1:], depth + 1):
                return True
        if re.fullmatch(r"remote\..+\.(?:push|mirror)", key) and (
            value.startswith("+") or value.lower() == "true"
        ):
            return True
    if not rest or rest[0].lower() != "push":
        return False
    for word in rest[1:]:
        if word.startswith("--"):
            if word.split("=", 1)[0] in (
                "--force", "--force-with-lease", "--force-if-includes", "--mirror",
            ):
                return True
        elif word.startswith("-"):
            if "f" in word[1:]:
                return True
        elif word.startswith("+"):
            return True
    return False


def tripped(line: str, depth: int = 0) -> bool:
    """Whether ``line`` contains a command that must prompt in every mode."""
    if _is_fork_bomb(line):
        return True
    for words in segments(line):
        if _deletes_root_or_home(words):
            return True
        for i, word in enumerate(words):
            if command_name(word) == "git" and _forces_push(words[i + 1:], depth):
                return True
    return False
