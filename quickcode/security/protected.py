"""Protected paths: which locations always ask before an allow rule applies.

A path is protected when it is outside the project root, or when any component
names ``.git``, ``.quickcode``, ``.ssh``, ``.env`` or ``.env.*``. The test runs
twice -- on the path as it was *written* and on the path as it *resolves* -- and
either one is enough.

Resolution alone was the whole check, so a name was only protected if it was
still there after symlinks were followed: ``.quickcode/artifacts/x.md`` linked
to a file elsewhere read as that file, and the artifact exception the engine
grants reads under that directory then applied to wherever the link pointed.
The written form matters as much as the target, so both are tested.

Names are compared the way Windows compares them, on every platform, because
on Windows ``.GIT``, ``.git.``, ``.git::$INDEX_ALLOCATION`` and ``GIT~1`` all open
``.git`` -- and NTFS upcases before comparing, so ``.gıt`` (dotless i) does too.
Being as strict on Linux costs nothing: nobody names a directory ``.GIT`` there.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

PROTECTED_DIRS = frozenset({".GIT", ".QUICKCODE", ".SSH"})

# An 8.3 short name that may stand for one of the protected names: the dotless
# stem cut to six characters (``GIT~1``, ``QUICKC~1``, ``ENV~1.LOC``), or the
# hashed form Windows falls back to after collisions (two letters, four hex).
_SHORT_NAME = re.compile(
    r"(?:GIT|QUICKC|SSH|ENV|(?:GI|QU|SS|EN)[0-9A-F]{4})~\d+(?:\.[^.]{0,3})?"
)
# Short-name spellings a glob's literal prefix could be the start of.
_SHORT_NAME_PREFIXES = ("GIT~", "QUICKC~", "SSH~", "ENV~")
# Names a glob that opens with a wildcard is tried against, where the shell
# lets such a glob match a leading dot. The `.env.*` family is open-ended, so
# it is represented by the spellings projects actually use.
_REPRESENTATIVE = (
    *PROTECTED_DIRS, ".ENV", *(f"{p}1" for p in _SHORT_NAME_PREFIXES),
    *(f".ENV.{s}" for s in (
        "LOCAL", "DEV", "DEVELOPMENT", "TEST", "STAGING", "PROD", "PRODUCTION",
        "EXAMPLE", "SAMPLE", "BAK", "BACKUP", "OLD", "SECRET", "CI",
    )),
)

# A shell word this side cannot resolve: a variable, a substitution, or a
# Windows %VAR%. The expansion happens in the shell, long after this decision,
# so `cat $HOME/.aws/credentials` is not a relative path inside the project --
# which is what it resolved as, and was auto-allowed as, until 2.4.1. Unknown
# is not safe.
UNRESOLVABLE = re.compile(r"\$[\w{(@*#?$!-]|`|%\w+%")

_SEPARATORS = re.compile(r"[\\/]+")


def canonical_name(part: str) -> str:
    """One path component as Windows would look it up.

    The stream suffix of an NTFS alternate data stream goes (``.env::$DATA``
    *is* ``.env``), trailing dots and spaces go (Win32 strips them), and the
    rest is upcased (NTFS compares case-insensitively).
    """
    return part.split(":", 1)[0].rstrip(". ").upper()


def is_protected_name(part: str) -> bool:
    name = canonical_name(part)
    if name in PROTECTED_DIRS or name == ".ENV" or name.startswith(".ENV."):
        return True
    return _SHORT_NAME.fullmatch(name) is not None


def glob_may_name_protected(component: str, *, dotfiles: bool) -> bool:
    """Whether a glob component could expand to a protected name.

    Decided on the literal text before the first wildcard where there is some:
    ``.e?v`` and ``.en*`` start the way ``.env`` does. A component that *opens*
    with a wildcard matches a leading dot only where the shell lets it
    (``dotfiles``) -- bash does not, PowerShell and cmd do -- and is then tried
    against the protected names, so ``*`` counts there and ``*.py`` does not.
    One that opens with a bracket expression is taken to be able to match,
    since the rules for it differ between shells.
    """
    first = min((i for i in (component.find(c) for c in "*?[") if i >= 0), default=-1)
    if first < 0:
        return is_protected_name(component)
    literal = component[:first].upper()
    if not literal:
        if component[0] == "[":
            return True
        pattern = component.upper()
        return dotfiles and any(fnmatch.fnmatchcase(n, pattern) for n in _REPRESENTATIVE)
    names = (*PROTECTED_DIRS, ".ENV", *_SHORT_NAME_PREFIXES)
    return any(name.startswith(literal) for name in names) or literal.startswith(".ENV.")


def lexical_parts(path: str) -> list[str]:
    """The components of a path as written, split on either separator."""
    return [p for p in _SEPARATORS.split(path) if p]


def resolve(path: str, base: Path) -> Path | None:
    """Where ``path`` lands, relative to ``base``; None when that is unknowable
    (an unknown ``~user``, a path the OS refuses to resolve)."""
    try:
        candidate = Path(path).expanduser()
        return (candidate if candidate.is_absolute() else base / candidate).resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def written_tail(path: str, root: Path) -> list[str]:
    """The written components below the project root.

    An absolute path spells the root's own components too, and those are not
    the caller's choice: a project that lives under a directory named
    ``.quickcode`` must not make every file in it protected.
    """
    parts = lexical_parts(path)
    for anchor in (root, root.resolve()):
        prefix = lexical_parts(str(anchor))
        if [p.upper() for p in parts[: len(prefix)]] == [p.upper() for p in prefix]:
            return parts[len(prefix):]
    return parts


def is_protected(path: str, root: Path, *, base: Path | None = None) -> bool:
    """True when ``path`` must prompt before any allow rule applies.

    ``base`` is what a relative path is relative to; the project root unless
    the caller knows better (a shell that has ``cd``-ed somewhere).
    """
    if UNRESOLVABLE.search(path):
        return True
    resolved = resolve(path, base or root)
    if resolved is None:
        return True
    if any(is_protected_name(p) for p in written_tail(path, root)):
        return True
    try:
        inside = resolved.relative_to(root.resolve())
    except ValueError:
        return True
    return any(is_protected_name(p) for p in inside.parts)


def is_subagent_artifact(path: str, root: Path) -> bool:
    """True for a path that resolves inside ``<root>/.quickcode/artifacts/``.

    Resolved exactly as ``is_protected`` resolves, so a symlink or a ``..``
    that lands outside the directory does not qualify. Callers apply this to
    *reads* only; writing here still goes through the ordinary prompt.
    """
    resolved = resolve(path, root)
    artifacts = resolve(".quickcode/artifacts", root)
    if resolved is None or artifacts is None:
        return False
    return resolved == artifacts or artifacts in resolved.parents
