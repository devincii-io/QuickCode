"""Git-worktree isolation for writer subagents.

Parallel writers in one checkout trample each other: two children editing the
same file interleave, and one child's half-finished edit is the other's broken
build. An isolated child gets a checkout of its own -- a detached git worktree
-- so parallel edits cannot collide by construction, and what it changed comes
back as a branch the spawner reviews and merges, never as edits already sitting
in the user's working tree.

**Where it lives.** ``<project>/.quickcode/worktrees/<agent_id>-<hex>``:

* inside the project's QuickCode directory, a protected path to every agent in
  the session, so nothing wanders in without a prompt;
* next to a ``.gitignore`` of ``*``, which keeps the worktrees out of the main
  checkout's ``git status`` and out of the ``glob`` walk (``grep`` skips
  ``.quickcode`` already) -- the parent's searches do not see every file twice;
* always under the *session's* project, even for a grandchild spawned by an
  isolated child, so removing one worktree never deletes another nested in it.

The suffix is random because agent ids restart at 1 in every conversation and
several conversations may share one project.

**Lifecycle.** The checkout exists while a run is in progress. ``open_tree``
creates it before a run -- at the spawner's HEAD, plus the spawner's
uncommitted changes to tracked files, committed there as a separate "carried"
commit so they never count as the child's work. ``settle`` runs when the run
ends however it ends: it commits what the run changed, points the branch
``quickcode/<name>`` at it, and removes the checkout. A resume
(``send_message``) reopens it at the branch tip. Between runs the only state is
the branch, which is the result: nothing lingers on disk, and a headless run
that simply exits leaves nothing behind but branches.

**What is never touched:** the user's current branch, index and working tree
(the carry-over reads them with ``git diff``, which writes nothing) and any
branch this module did not create. At conversation close ``close_all`` deletes
the branches that are fully merged into the project's HEAD -- through
``git branch -d``, which refuses an unmerged one -- and keeps and logs the rest.

Every git call goes through ``quickcode/gitcmd.py``: no hook, fsmonitor,
external diff, textconv or signing program the repository configures runs.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import secrets
import shutil
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from quickcode import gitcmd
from quickcode.config import Environment
from quickcode.core.events import WorktreeEvent
from quickcode.workspace import PROJECT_DIRNAME, ensure_project_dir

log = logging.getLogger("quickcode.subagents.worktree")

HOME = Path(PROJECT_DIRNAME) / "worktrees"
BRANCH_PREFIX = "quickcode/"
# Where the harness itself writes inside a checkout (a nested child's
# offloaded report lands in ``.quickcode/artifacts``). Never the child's work:
# the child cannot write there without a prompt it has nobody to answer.
HARNESS_DIR = PROJECT_DIRNAME

PROBE_TIMEOUT = 10.0
GIT_TIMEOUT = 60.0
CHECKOUT_TIMEOUT = 300.0
STAT_FILES = 40
SHORT = 12

_FALLBACK_NAME = "QuickCode"
_FALLBACK_EMAIL = "quickcode@localhost"

# Ref updates one at a time in this process: parallel spawns fan out, and two
# branch writes at once can collide on ``packed-refs.lock``. Checkouts and
# commits are not serialised -- each worktree has its own index and HEAD, and
# holding a lock across a checkout would park executor threads behind it.
_REFS = threading.Lock()

_SHORTSTAT = {
    "files": re.compile(r"(\d+) files? changed"),
    "insertions": re.compile(r"(\d+) insertions?\(\+\)"),
    "deletions": re.compile(r"(\d+) deletions?\(-\)"),
}


class IsolationError(ValueError):
    """Worktree isolation is not possible here, or one of its git steps failed.

    A ``ValueError`` so a spawn refused for it comes back as the same tool
    error every other refusal does.
    """


@dataclass(frozen=True)
class Probe:
    """The spawner's checkout, read before anything is created."""

    source: Path
    # The spawner's directory below the repository's top level ("" at the top):
    # a project nested in a larger repository works in the same subdirectory
    # of its worktree.
    prefix: str
    head: str
    branch: str


@dataclass
class Settlement:
    """How one run's work was left."""

    action: str  # committed | unchanged | kept | failed
    files: int = 0
    insertions: int = 0
    deletions: int = 0
    stat: str = ""
    detail: str = ""


@dataclass
class Worktree:
    """One isolated child's checkout, across every run it gets."""

    agent_id: str
    name: str
    # The directory ``.quickcode/worktrees`` lives in: the session's project.
    home: Path
    origin: Probe
    # Where the child's own work starts: the spawner's HEAD, or the commit that
    # carried the spawner's uncommitted changes over. Empty until first opened.
    base: str = ""
    # The last commit settled onto ``branch``.
    tip: str = ""
    branch: str = ""
    carried: int = 0
    untracked: int = 0
    last: Settlement | None = None

    @property
    def path(self) -> Path:
        return self.home / HOME / self.name

    @property
    def cwd(self) -> Path:
        """Where the child works: the spawner's own directory, in the copy."""
        return self.path / self.origin.prefix if self.origin.prefix else self.path

    @property
    def active(self) -> bool:
        return self.path.exists()

    @property
    def intended_branch(self) -> str:
        return BRANCH_PREFIX + self.name


# --------------------------------------------------------------------------
# before a spawn
# --------------------------------------------------------------------------


def probe(source: Path) -> Probe:
    """Refuse, or describe the checkout a worktree would be made from.

    Cheap on purpose -- three ``rev-parse``-sized calls -- because it runs
    while the spawn is still refusable, before the child exists.
    """
    top = gitcmd.run(source, "rev-parse", "--show-toplevel", "--show-prefix",
                     timeout=PROBE_TIMEOUT)
    if not top.ok:
        raise IsolationError(
            f"worktree isolation needs a git repository, and {source} is not inside "
            f"one ({top.reason()}). Spawn the subagent without isolation."
        )
    lines = top.stdout.splitlines()
    prefix = lines[1].strip().rstrip("/") if len(lines) > 1 else ""
    head = gitcmd.run(source, "rev-parse", "--verify", "--quiet", "HEAD^{commit}",
                      timeout=PROBE_TIMEOUT)
    if not head.ok or not head.stdout.strip():
        raise IsolationError(
            "worktree isolation branches from the current commit, and this "
            "repository has no commits yet. Spawn the subagent without isolation."
        )
    branch = gitcmd.run(source, "symbolic-ref", "--quiet", "--short", "HEAD",
                        timeout=PROBE_TIMEOUT)
    return Probe(
        source=source, prefix=prefix, head=head.stdout.strip(),
        branch=branch.stdout.strip() if branch.ok else "",
    )


def plan(origin: Probe, *, home: Path, agent_id: str) -> Worktree:
    """Name the worktree a child will get. Creates nothing."""
    for _ in range(16):
        tree = Worktree(agent_id=agent_id, name=f"{agent_id}-{secrets.token_hex(2)}",
                        home=home, origin=origin)
        if not tree.path.exists():
            return tree
    raise IsolationError("could not find an unused worktree name")


def child_environment(env: Environment, tree: Worktree) -> Environment:
    """The environment block a child in ``tree`` is told about."""
    return dataclasses.replace(
        env, cwd=str(tree.cwd), is_git_repo=True,
        git_branch="(detached, in an isolated worktree)",
    )


def prompt_text(tree: Worktree) -> str:
    """The child's ``<isolation>`` block: where it is and where its work goes."""
    origin = tree.origin
    at = f"commit {origin.head[:SHORT]}"
    if origin.branch:
        at += f" (the tip of {origin.branch})"
    return (
        f"You are working in your own git worktree, {tree.cwd}: a separate checkout\n"
        "of this repository made for you alone, so nothing you do here can collide\n"
        "with other agents or with the checkout you were spawned from. It starts at\n"
        f"{at} plus that checkout's uncommitted changes to tracked files;\n"
        "untracked files were not copied.\n"
        f"That checkout ({origin.source}) is outside your reach. Paths in your task\n"
        "that point into it name the same files here, relative to your working\n"
        "directory.\n"
        "When you finish, everything you changed here is committed to the branch\n"
        f"{tree.intended_branch} for your spawner to review and merge. You do not\n"
        "need to commit; do not switch branches, reset or push."
    )


# --------------------------------------------------------------------------
# around a run
# --------------------------------------------------------------------------


def open_tree(tree: Worktree) -> str:
    """Make sure the checkout exists. Returns ``created``, ``reopened`` or ""
    (it was still there). Blocking; run it off the event loop."""
    if tree.active:
        return ""
    _prepare_home(tree.home)
    start = tree.tip or tree.base or tree.origin.head
    # From the session's project, which always exists -- a nested child's
    # spawner works in a worktree that may be gone by the time it resumes.
    # ``--force`` only overrides "registered but missing", for this path,
    # which is what a checkout deleted by hand between runs leaves behind.
    added = gitcmd.run(tree.home, "worktree", "add", "--force", "--detach",
                       str(tree.path), start, write=True, timeout=CHECKOUT_TIMEOUT)
    if not added.ok:
        _discard(tree)
        raise IsolationError(f"could not create the worktree: {added.reason()}")
    if tree.base:
        return "reopened"
    try:
        _carry(tree)
    except IsolationError:
        _discard(tree)
        raise
    return "created"


def settle(tree: Worktree) -> Settlement:
    """Commit what the run changed, move the branch, remove the checkout.

    Never raises: how the work was left is the answer, including "it could not
    be committed, and the checkout is kept where it is".
    """
    if not tree.active:
        tree.last = tree.last or Settlement("unchanged")
        return tree.last
    try:
        result = _settle(tree)
    except IsolationError as exc:
        result = Settlement("failed", detail=str(exc))
    tree.last = result
    return result


def event(tree: Worktree, action: str, settlement: Settlement | None = None) -> WorktreeEvent:
    """The session-log record of one step."""
    s = settlement or Settlement(action)
    return WorktreeEvent(
        action=action,  # type: ignore[arg-type]
        path=str(tree.path), branch=tree.branch, base=tree.base, commit=tree.tip,
        files=s.files, insertions=s.insertions, deletions=s.deletions, detail=s.detail,
    )


def report_block(tree: Worktree) -> str:
    """What the spawner reads under the child's report.

    Appended by the harness after the report is sanitized, and ``worktree`` is
    one of the tags the sanitizer defuses, so a child cannot write one of its
    own naming some other branch.
    """
    s = tree.last or Settlement("unchanged")
    lines: list[str] = []
    if tree.branch:
        attrs = (f'branch="{tree.branch}" base="{tree.base[:SHORT]}" files="{s.files}" '
                 f'insertions="{s.insertions}" deletions="{s.deletions}"')
        if s.stat:
            lines.append(s.stat)
        review = f"git diff {tree.base[:SHORT]} {tree.branch}"
        if tree.carried:
            lines.append(
                f"The branch's first commit ({tree.base[:SHORT]}) holds your uncommitted "
                f"changes to {tree.carried} tracked file(s) as they were at spawn, so the "
                "subagent started where you stood; the stat above is only its own work. "
                f"Review with `{review}`; bring in just that work with `git cherry-pick "
                f"{tree.base[:SHORT]}..{tree.branch}`, or `{review} | git apply` to leave "
                "it uncommitted."
            )
        else:
            lines.append(f"Review with `{review}`; bring it in with `git merge {tree.branch}`.")
    else:
        attrs = 'changes="none"'
        lines.append("The subagent changed nothing.")
    if tree.untracked:
        lines.append(f"{tree.untracked} untracked file(s) in your checkout were not carried over.")
    if s.action == "failed":
        lines.append(f"Its latest changes could not be committed ({s.detail}); they are "
                     f"still in the worktree at {tree.path}, nothing was discarded.")
    elif s.action == "kept":
        lines.append(f"The worktree could not be removed and is kept at {tree.path} "
                     f"({s.detail}).")
    elif tree.branch:
        lines.append("Its worktree has been removed; the branch is the result.")
    else:
        lines.append("Its worktree has been removed.")
    body = "\n".join(lines)
    return f"<worktree {attrs}>\n{body}\n</worktree>"


# --------------------------------------------------------------------------
# conversation close
# --------------------------------------------------------------------------


def close_all(trees: Iterable[Worktree]) -> None:
    """Settle every checkout still on disk and delete the branches that are
    fully merged into the project's HEAD. Blocking; never raises.

    A branch that is not merged is kept, and so is a checkout that could not
    be removed; both are logged with where they are.
    """
    for tree in list(trees):
        try:
            if tree.active:
                s = settle(tree)
                if s.action in ("kept", "failed"):
                    log.warning("subagent worktree %s kept at %s: %s",
                                tree.agent_id, tree.path, s.detail)
            if not tree.branch:
                continue
            with _REFS:
                gone = gitcmd.run(tree.home, "branch", "-d", tree.branch, write=True,
                                  timeout=GIT_TIMEOUT)
            if gone.ok:
                log.info("deleted merged subagent branch %s", tree.branch)
            else:
                log.info("kept subagent branch %s (%s): %s",
                         tree.branch, tree.agent_id, gone.reason())
        except Exception:  # noqa: BLE001 -- one tree must not stop the rest
            log.exception("could not clean up the worktree of %s", tree.agent_id)


# --------------------------------------------------------------------------
# the git steps
# --------------------------------------------------------------------------


_IGNORE_TEXT = """\
# Written by QuickCode. Subagent worktrees are scratch checkouts of this
# repository (subagents/worktree.py): their work is on quickcode/* branches,
# and without this every file in them would show up in `git status` and in
# searches of the project a second time.
*
"""


def _prepare_home(home: Path) -> None:
    where = ensure_project_dir(home) / "worktrees"
    where.mkdir(exist_ok=True)
    ignore = where / ".gitignore"
    if not ignore.exists():
        ignore.write_text(_IGNORE_TEXT, encoding="utf-8")


def _discard(tree: Worktree) -> None:
    """Undo a checkout that failed halfway. Only ever this module's own path."""
    gitcmd.run(tree.home, "worktree", "remove", "--force", str(tree.path), write=True,
               timeout=GIT_TIMEOUT)
    if tree.path.exists() and tree.path.parent == tree.home / HOME:
        shutil.rmtree(tree.path, ignore_errors=True)


def _carry(tree: Worktree) -> None:
    """Bring the spawner's uncommitted changes to tracked files along.

    Read with ``git diff`` against the probed commit, which writes nothing in
    the spawner's checkout (``git stash create`` would refresh its index), and
    compared against that commit rather than ``HEAD`` so a commit landing in
    between cannot hand ``git apply`` a patch for a different base. The
    prefix options pin a patch shape that user config (``diff.noprefix``,
    ``diff.relative``, ``color.diff``) would otherwise change under ``apply``.
    """
    origin = tree.origin
    diff = gitcmd.run(
        origin.source, "diff", *gitcmd.DIFF_SAFE, "--binary", "--no-color",
        "--no-relative", "--ignore-submodules", "--src-prefix=a/", "--dst-prefix=b/",
        origin.head, raw=True, timeout=GIT_TIMEOUT,
    )
    if not diff.ok:
        raise IsolationError(f"could not read the uncommitted changes: {diff.reason()}")
    others = gitcmd.run(origin.source, "ls-files", "--others", "--exclude-standard", "-z",
                        timeout=GIT_TIMEOUT)
    tree.untracked = sum(
        1 for p in others.stdout.split("\0")
        if p and not p.startswith(HARNESS_DIR + "/")
    )
    if diff.data.strip():
        applied = gitcmd.run(tree.path, "apply", "--binary", "--whitespace=nowarn", "-",
                             stdin=diff.data, write=True, timeout=GIT_TIMEOUT)
        if not applied.ok:
            raise IsolationError(
                "could not carry the uncommitted changes into the worktree: "
                f"{applied.reason()}. Commit or stash them first, or spawn without isolation."
            )
    if not diff.data.strip() or not _stage(tree, drop_harness=False):
        tree.base = origin.head
        return
    _commit(tree, f"quickcode: uncommitted changes carried into {tree.name}\n\n"
                  f"The spawner's working tree on top of {origin.head[:SHORT]}, so the "
                  "subagent starts where its spawner stood. Not part of the subagent's "
                  "own work.")
    tree.base = tree.tip = _rev(tree.path, "HEAD")
    tree.carried = diff.data.count(b"\ndiff --git ") + diff.data.startswith(b"diff --git ")


def _settle(tree: Worktree) -> Settlement:
    if _stage(tree, drop_harness=True):
        _commit(tree, f"quickcode: work by subagent {tree.agent_id}")
    head = _rev(tree.path, "HEAD")
    moved = head != (tree.tip or tree.base)
    if moved:
        _point_branch(tree, head)
    result = Settlement("committed" if moved else "unchanged")
    if tree.branch:
        _measure(tree, result)
    leftovers = _leftovers(tree)
    if leftovers:
        result.action = "kept"
        result.detail = f"uncommitted paths remain: {', '.join(leftovers[:5])}"
        return result
    removed = gitcmd.run(tree.home, "worktree", "remove", "--force", str(tree.path),
                         write=True, timeout=GIT_TIMEOUT)
    if not removed.ok:
        result.action = "kept"
        result.detail = removed.reason()
    return result


def _stage(tree: Worktree, *, drop_harness: bool) -> bool:
    """Stage everything in the checkout; True when anything differs from HEAD."""
    # ``safecrlf`` would refuse the add outright over a line ending, which is a
    # policy for a person at a prompt, not for a commit nobody is watching.
    added = gitcmd.run(tree.path, "-c", "core.safecrlf=false", "add", "--all",
                       write=True, timeout=GIT_TIMEOUT)
    if not added.ok:
        raise IsolationError(f"git add failed: {added.reason()}")
    if drop_harness:
        gitcmd.run(tree.cwd, "reset", "--quiet", "--", HARNESS_DIR, write=True,
                   timeout=GIT_TIMEOUT)
    staged = gitcmd.run(tree.path, "diff", "--cached", "--quiet", "--ignore-submodules",
                        timeout=GIT_TIMEOUT)
    if staged.code not in (0, 1):
        raise IsolationError(f"could not read the staged changes: {staged.reason()}")
    return staged.code == 1


def _commit(tree: Worktree, message: str) -> None:
    identity: list[str] = []
    for key, fallback in (("user.name", _FALLBACK_NAME), ("user.email", _FALLBACK_EMAIL)):
        if not gitcmd.run(tree.path, "config", "--get", key).stdout.strip():
            identity += ["-c", f"{key}={fallback}"]
    done = gitcmd.run(tree.path, *identity, "commit", "--quiet", "--no-verify", "-m", message,
                      write=True, timeout=GIT_TIMEOUT)
    if not done.ok:
        raise IsolationError(f"git commit failed: {done.reason()}")


def _rev(cwd: Path, ref: str) -> str:
    out = gitcmd.run(cwd, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if not out.ok or not out.stdout.strip():
        raise IsolationError(f"could not resolve {ref}: {out.reason()}")
    return out.stdout.strip()


def _point_branch(tree: Worktree, head: str) -> None:
    """Advance this tree's branch to ``head``, or start a new one.

    Advancing is compare-and-swap against the tip this module last set, and
    only while nobody has the branch checked out: a branch the user moved,
    deleted or checked out is theirs now, and the work goes to a fresh name
    rather than over whatever they did.
    """
    with _REFS:
        _point_branch_locked(tree, head)


def _point_branch_locked(tree: Worktree, head: str) -> None:
    if tree.branch and tree.tip and not _checked_out(tree, tree.branch):
        moved = gitcmd.run(tree.home, "update-ref", "-m", f"quickcode: {tree.agent_id}",
                           f"refs/heads/{tree.branch}", head, tree.tip, write=True,
                           timeout=GIT_TIMEOUT)
        if moved.ok:
            tree.tip = head
            return
    for n in range(1, 50):
        name = tree.intended_branch if n == 1 else f"{tree.intended_branch}-{n}"
        # ``git branch`` refuses a name that exists, so this never moves a
        # branch it did not just create.
        made = gitcmd.run(tree.home, "branch", "--no-track", name, head, write=True,
                          timeout=GIT_TIMEOUT)
        if made.ok:
            tree.branch, tree.tip = name, head
            return
    raise IsolationError(f"could not create a branch for the work: {made.reason()}")


def _checked_out(tree: Worktree, branch: str) -> bool:
    listing = gitcmd.run(tree.home, "worktree", "list", "--porcelain", timeout=GIT_TIMEOUT)
    return f"branch refs/heads/{branch}" in listing.stdout.splitlines()


def _measure(tree: Worktree, result: Settlement) -> None:
    """``git diff --stat`` of the child's own work: base to the branch tip."""
    span = (tree.base, tree.tip)
    short = gitcmd.run(tree.home, "diff", *gitcmd.DIFF_SAFE, "--no-color", "--shortstat",
                       *span, timeout=GIT_TIMEOUT)
    for key, pattern in _SHORTSTAT.items():
        m = pattern.search(short.stdout)
        setattr(result, key, int(m.group(1)) if m else 0)
    stat = gitcmd.run(tree.home, "diff", *gitcmd.DIFF_SAFE, "--no-color", "--no-relative",
                      "--stat=100", f"--stat-count={STAT_FILES}", *span, timeout=GIT_TIMEOUT)
    result.stat = stat.stdout.rstrip()


def _leftovers(tree: Worktree) -> list[str]:
    """Paths still differing from HEAD after the commit, the harness's own
    directory aside -- anything here would be lost with the checkout."""
    status = gitcmd.run(tree.path, "status", "--porcelain", "-z", "--untracked-files=all",
                        "--ignore-submodules=all", timeout=GIT_TIMEOUT)
    if not status.ok:
        return [f"(git status failed: {status.reason()})"]
    harness = f"{tree.origin.prefix}/{HARNESS_DIR}/" if tree.origin.prefix else f"{HARNESS_DIR}/"
    paths = [entry[3:] for entry in status.stdout.split("\0") if len(entry) > 3]
    return [p for p in paths if not p.startswith(harness)]
