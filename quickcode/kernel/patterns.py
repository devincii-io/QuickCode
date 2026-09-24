"""Name patterns the way the kernel reads ``tools:``, ``spawns:`` and ``models:``.

An entry is either a literal name or an ``fnmatch`` glob, matched
case-sensitively. The resolver, the used-by index and the agent workbench all
ask the same two questions of an entry, so the answers live here once: a
workbench that called ``task_*`` a literal while the resolver expanded it would
report a grant differently from how it was made.
"""

from __future__ import annotations

from fnmatch import fnmatchcase

GLOB_CHARS = ("*", "?", "[")


def is_glob(pattern: str) -> bool:
    """Whether ``pattern`` can match a name other than itself."""
    return any(ch in pattern for ch in GLOB_CHARS)


def pattern_matches(pattern: str, name: str) -> bool:
    return pattern == name or fnmatchcase(name, pattern)
