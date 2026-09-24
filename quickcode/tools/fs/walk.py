"""The directory walk behind glob and grep's pure-Python search.

``Path.rglob`` was doing this job and got four things wrong for a search tool:

* **Order.** It yields in directory order, which on most filesystems is hash
  order, so the same search returned its results shuffled between runs.
* **Pruning.** It walked all of ``node_modules`` and ``.git`` and the caller
  threw the results away afterwards -- the cost of the search was the cost of
  the directories it was told to ignore.
* **Relative exclusions.** The caller tested every *absolute* path component,
  so a project that happened to live under a directory called ``venv`` or
  ``node_modules`` -- or a search the model aimed *into* one on purpose --
  found nothing at all.
* **Ignore files.** ``.gitignore`` was not read, while ripgrep, the other
  backend, honours it: the same grep answered differently depending on
  whether ripgrep was installed.

This walk visits entries in name order (so paths come out in component-wise
order, the order ``grep.path_key`` sorts ripgrep's output into), prunes
excluded directories before entering them, never descends a symlinked
directory (so a link cycle cannot loop), and applies ignore files the way
ripgrep does: ``.gitignore`` only inside a git repository, ``.ignore`` and
``.rgignore`` everywhere, ``.git/info/exclude`` at the repository root, and
the ignore files of the directories above the starting point as well as
below it. The starting directory itself is never tested -- naming a directory
is asking for it, as it is to ripgrep.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from quickcode.tools.fs.patterns import IgnoreRule, parse_ignore

# Never entered by glob or grep, at any depth below where the walk starts.
# Starting *inside* one is fine: exclusions apply to what the walk finds, not
# to where it was sent.
IGNORED_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache",
})

GITIGNORE = ".gitignore"
# Honoured with or without a repository, after .gitignore so they win.
PLAIN_IGNORE_FILES = (".ignore", ".rgignore")

Exclude = Callable[[str, bool], bool]


@dataclass(frozen=True)
class _RuleSet:
    """One ignore file's rules and where they apply.

    A path relative to the walk's start becomes a path relative to the ignore
    file's directory by dropping ``strip`` (for a file below the start) or
    adding ``prepend`` (for one above it).
    """

    rules: list[IgnoreRule]
    strip: str = ""
    prepend: str = ""


def repo_root(start: Path) -> Path | None:
    """The nearest directory at or above ``start`` holding ``.git``."""
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def walk_files(
    start: Path,
    *,
    exclude: Exclude | None = None,
    hidden: bool = True,
    ignore_files: bool = True,
    follow_file_links: bool = False,
    max_depth: int | None = None,
) -> Iterator[tuple[str, Path]]:
    """Every file under ``start``, in path order, as ``(relative path, Path)``.

    ``exclude(name, is_dir)`` drops an entry by name, and a dropped directory
    is never entered. ``hidden=False`` drops dot-entries the same way.
    Symlinked directories are never followed; symlinked files are yielded only
    with ``follow_file_links`` (ripgrep skips them by default, a glob lists
    them). ``max_depth`` bounds how many path components a yielded path may
    have, so ``*.py`` does not walk the whole tree to answer about one level.
    """
    start = Path(start)
    repo = repo_root(start) if ignore_files else None
    walk = _Walk(exclude, hidden, ignore_files, follow_file_links, repo is not None, max_depth,
                 visited=set())
    inherited = _ancestor_rules(start, repo) if ignore_files else []
    yield from walk.visit(start, "", inherited, 1)


@dataclass(frozen=True)
class _Walk:
    exclude: Exclude | None
    hidden: bool
    ignore_files: bool
    follow_file_links: bool
    in_repo: bool
    max_depth: int | None
    # (device, inode) of every directory entered. Links are never followed,
    # but a bind mount or a filesystem that reports no links can still bring
    # a walk back to where it has been; this is what stops it looping.
    visited: set[tuple[int, int]]

    def visit(
        self, directory: Path, rel: str, rules: list[_RuleSet], depth: int
    ) -> Iterator[tuple[str, Path]]:
        try:
            st = os.stat(directory)
        except OSError:
            return
        if st.st_ino:
            key = (st.st_dev, st.st_ino)
            if key in self.visited:
                return
            self.visited.add(key)
        if self.ignore_files:
            local = _load_rules(directory, self.in_repo)
            if local:
                rules = [*rules, _RuleSet(local, strip=rel)]
        for child, path, is_dir in self._entries(directory, rel, rules):
            if not is_dir:
                yield child, path
            elif self.max_depth is None or depth < self.max_depth:
                yield from self.visit(path, child + "/", rules, depth + 1)

    def _entries(
        self, directory: Path, rel: str, rules: list[_RuleSet]
    ) -> Iterator[tuple[str, Path, bool]]:
        try:
            with os.scandir(directory) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            name = entry.name
            if not self.hidden and name.startswith("."):
                continue
            child = f"{rel}{name}"
            try:
                # A junction is Windows' directory link, and is_symlink() says
                # False for it -- pnpm's node_modules is made of them.
                if entry.is_symlink() or entry.is_junction():
                    if not self.follow_file_links or not entry.is_file():
                        continue
                    is_dir = False
                else:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    if not is_dir and not entry.is_file(follow_symlinks=False):
                        continue  # sockets, fifos, devices
            except OSError:
                continue
            if self.exclude is not None and self.exclude(name, is_dir):
                continue
            if rules and _ignored(child, is_dir, rules):
                continue
            yield child, Path(entry.path), is_dir


def _ignored(rel: str, is_dir: bool, rule_sets: list[_RuleSet]) -> bool:
    """Last matching rule wins, deeper ignore files after shallower ones."""
    verdict = False
    for rule_set in rule_sets:
        if rule_set.strip and not rel.startswith(rule_set.strip):
            continue
        sub = rule_set.prepend + rel[len(rule_set.strip):]
        for rule in rule_set.rules:
            if rule.dir_only and not is_dir:
                continue
            if rule.regex.match(sub):
                verdict = not rule.negate
    return verdict


def _load_rules(directory: Path, in_repo: bool) -> list[IgnoreRule]:
    names = ((GITIGNORE,) if in_repo else ()) + PLAIN_IGNORE_FILES
    rules: list[IgnoreRule] = []
    for name in names:
        try:
            text = (directory / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rules.extend(parse_ignore(text))
    return rules


def _ancestor_rules(start: Path, repo: Path | None) -> list[_RuleSet]:
    """The ignore files above ``start`` that still govern what is below it."""
    try:
        start = start.resolve()
    except OSError:
        return []
    top = repo.resolve() if repo is not None else None
    chain = [p for p in reversed(start.parents) if top is None or p == top or top in p.parents]
    if top is None:
        chain = []  # outside a repository only .ignore files apply, and only below
    sets: list[_RuleSet] = []
    if top is not None:
        exclude_file = top / ".git" / "info" / "exclude"
        try:
            rules = parse_ignore(exclude_file.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            rules = []
        if rules:
            sets.append(_RuleSet(rules, prepend=_between(top, start)))
    for directory in chain:
        rules = _load_rules(directory, in_repo=True)
        if rules:
            sets.append(_RuleSet(rules, prepend=_between(directory, start)))
    return sets


def _between(ancestor: Path, start: Path) -> str:
    rel = start.relative_to(ancestor).as_posix()
    return "" if rel == "." else rel + "/"
