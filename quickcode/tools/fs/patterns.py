"""Glob patterns and ignore files, compiled to regular expressions.

Two pattern languages, one translator. A glob tool pattern (``src/**/*.{ts,tsx}``)
and a ``.gitignore`` line (``/build/``, ``!keep.log``) share their wildcards:
``*`` and ``?`` stop at a slash, ``**`` as a whole segment crosses any number
of directories, ``[...]`` is a character class. Braces are glob-only -- git has
no brace expansion, and a ``{`` in an ignore file is a literal brace.

Everything here matches relative, slash-separated paths. Callers turn OS paths
into that form before asking.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

MAGIC = frozenset("*?[{")
# Expansion of ``{a,b}{c,d}...`` is a product; this caps it well above any
# pattern a person writes and well below one written to hurt.
MAX_BRACE_EXPANSIONS = 256
# Windows filesystems are case-insensitive, and so is git on them by default.
CASE_FLAGS = re.IGNORECASE if os.name == "nt" else 0


class PatternError(ValueError):
    pass


def has_magic(segment: str) -> bool:
    return any(ch in MAGIC for ch in segment)


def expand_braces(pattern: str) -> list[str]:
    """``a{b,c}d`` -> ``[abd, acd]``, nested braces included.

    A brace with no top-level comma, or one never closed, is literal -- the
    same as bash, and the only reading that cannot turn a filename containing
    a brace into a pattern that matches nothing.
    """
    out = _expand(pattern)
    if len(out) > MAX_BRACE_EXPANSIONS:
        raise PatternError(f"the braces in {pattern!r} expand to more than "
                           f"{MAX_BRACE_EXPANSIONS} patterns")
    return out


def _expand(pattern: str) -> list[str]:
    depth = 0
    start = -1
    commas: list[int] = []
    for i, ch in enumerate(pattern):
        if ch == "\\":
            continue
        if ch == "{":
            if depth == 0:
                start, commas = i, []
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0:
                if not commas:
                    rest = _expand(pattern[i + 1:])
                    return [pattern[: i + 1] + tail for tail in rest]
                head, tail = pattern[:start], pattern[i + 1:]
                bounds = [start, *commas, i]
                options = [pattern[a + 1:b] for a, b in zip(bounds, bounds[1:], strict=False)]
                tails = _expand(tail)
                out = []
                for option in options:
                    for middle in _expand(option):
                        out.extend(head + middle + t for t in tails)
                        if len(out) > MAX_BRACE_EXPANSIONS:
                            return out
                return out
        elif ch == "," and depth == 1:
            commas.append(i)
    return [pattern]


def translate(pattern: str) -> str:
    """Regex source matching the whole of a relative path. No braces here --
    expand them first."""
    segments = pattern.split("/")
    parts: list[str] = []
    for i, segment in enumerate(segments):
        last = i == len(segments) - 1
        if segment == "**":
            parts.append(".*" if last else "(?:.*/)?")
        else:
            parts.append(_segment(segment) + ("" if last else "/"))
    return "".join(parts)


def _segment(segment: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(segment):
        ch = segment[i]
        if ch == "\\" and i + 1 < len(segment):
            out.append(re.escape(segment[i + 1]))
            i += 2
            continue
        if ch == "*":
            while i + 1 < len(segment) and segment[i + 1] == "*":
                i += 1
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        elif ch == "[":
            end = _class_end(segment, i)
            if end < 0:
                out.append(re.escape(ch))
            else:
                body = segment[i + 1:end]
                negate = body[:1] in ("!", "^")
                if negate:
                    body = body[1:]
                body = body.replace("\\", "\\\\")
                out.append(f"[{'^' if negate else ''}{body}]")
                i = end
        else:
            out.append(re.escape(ch))
        i += 1
    return "".join(out)


def _class_end(segment: str, start: int) -> int:
    """Index of the ``]`` closing the class opened at ``start``, or -1."""
    i = start + 1
    if i < len(segment) and segment[i] in "!^":
        i += 1
    if i < len(segment) and segment[i] == "]":
        i += 1
    while i < len(segment):
        if segment[i] == "]":
            return i
        i += 1
    return -1


def compile_glob(pattern: str, *, anywhere: bool = False) -> re.Pattern[str]:
    """A glob as a regex over relative paths.

    ``anywhere`` is ripgrep's ``--glob`` rule: a pattern with no slash in it
    matches a file's name at any depth, as ``*.py`` is universally meant.
    """
    sources = []
    for expanded in expand_braces(pattern):
        source = translate(expanded)
        if anywhere and "/" not in expanded:
            source = "(?:.*/)?" + source
        sources.append(source)
    try:
        return re.compile("(?:" + "|".join(sources) + r")\Z", CASE_FLAGS | re.DOTALL)
    except re.error as exc:
        raise PatternError(f"invalid pattern {pattern!r}: {exc}") from None


@dataclass(frozen=True)
class IgnoreRule:
    regex: re.Pattern[str]
    negate: bool
    dir_only: bool


def parse_ignore(text: str) -> list[IgnoreRule]:
    """The rules in one ``.gitignore`` (or ``.ignore``) file, in file order.

    gitignore(5): ``#`` starts a comment, ``!`` negates, a trailing ``/``
    matches only directories, and a pattern containing a slash anywhere but at
    its end is anchored to the file's own directory -- one without matches at
    any depth below it.
    """
    rules: list[IgnoreRule] = []
    for line in text.splitlines():
        line = _strip_trailing_spaces(line)
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        if negate:
            line = line[1:]
        elif line.startswith(("\\#", "\\!")):
            line = line[1:]
        dir_only = line.endswith("/")
        line = line.rstrip("/")
        if not line:
            continue
        anchored = "/" in line
        line = line.lstrip("/")
        source = translate(line)
        if not anchored:
            source = "(?:.*/)?" + source
        try:
            regex = re.compile(source + r"\Z", CASE_FLAGS | re.DOTALL)
        except re.error:
            continue  # git ignores a line it cannot parse; so does this
        rules.append(IgnoreRule(regex, negate, dir_only))
    return rules


def _strip_trailing_spaces(line: str) -> str:
    stripped = line.rstrip(" ")
    if stripped.endswith("\\") and len(stripped) < len(line):
        return stripped[:-1] + " "
    return stripped
