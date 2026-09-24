"""Running git without running the repository's own code.

A repository's configuration can name programs that git runs on its behalf: an
fsmonitor hook on every status, external diff and textconv drivers on every
diff, hooks on commit, checkout and every ref update, a signing program on
commit. QuickCode runs git the moment a project opens (the git panel,
``server/gitinfo.py``) and on a subagent's behalf (worktree isolation,
``subagents/worktree.py``) -- before anyone has necessarily trusted the
project -- so none of those may run. The options that say so live here, once,
so the two callers cannot drift apart about what "safe" means.

Content filters (``filter.<name>.clean`` / ``smudge`` / ``process``) are the
one kind the repository names itself: ``.gitattributes`` picks the name, so no
fixed option covers them. Before each call that can touch file content, the
filters the *repository's* config defines are listed and switched off by name
(``filter_overrides``). A filter defined in the user's or the system's config
-- which is how Git LFS is set up -- is the user's program and keeps running.

What this does not do, deliberately: set ``GIT_CONFIG_NOSYSTEM``. The system
config is written by an administrator or by Git's installer, not by a
repository, and Git for Windows keeps ``core.autocrlf`` and the LFS filter
there -- without it every CRLF file diffs as wholly changed. Every key that
would run a program is overridden on the command line instead, which outranks
the system config as it does the repository's.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from quickcode import subproc

# Options every call carries. No fsmonitor hook, and ``--literal-pathspecs``
# because a path proved to be inside the project would otherwise still be read
# as pathspec magic -- ``:(top)``/``:/`` name the repository root, which for a
# project nested in a larger repository is above it. And no optional locks, so
# a status refresh never leaves the user's own ``git commit`` failing on a held
# ``index.lock``. The ``ext::`` transport runs a command line, and nothing
# QuickCode runs has a reason to reach a remote at all; no submodule recursion
# for commands that would otherwise update or fetch into submodules.
BASE = (
    "--literal-pathspecs", "--no-optional-locks",
    "-c", "core.quotepath=off", "-c", "core.fsmonitor=false",
    "-c", "protocol.ext.allow=never", "-c", "submodule.recurse=false",
)
# ``git status`` and ``git diff`` look inside a submodule by running git there,
# with the submodule's own config, whose filters the list below never saw.
# ``dirty`` still reports a submodule on a different commit. It has to be the
# flag: ``diff.ignoreSubmodules`` is overridden per submodule by an ``ignore``
# line the repository's own ``.gitmodules`` can carry, and the flag is not.
STATUS_SAFE = ("--ignore-submodules=dirty",)
# External diff and textconv drivers stay out of every diff.
DIFF_SAFE = ("--no-ext-diff", "--no-textconv", *STATUS_SAFE)
# For commands that write: no hook runs (``post-checkout`` on a worktree add,
# ``pre-commit`` and friends on a commit, ``reference-transaction`` on any ref
# update) and no signing program is started -- one waiting on a passphrase
# prompt nobody can see would hang the call until its timeout.
WRITE_SAFE = (
    "-c", f"core.hooksPath={os.devnull}",
    "-c", "commit.gpgsign=false",
)

# Environment variables that would point git at a different repository, index
# or object store than the directory it is run in. Inherited from whatever
# launched the app (a git hook, a wrapper script), they would make a write land
# somewhere nobody named.
_REDIRECTING_ENV = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
)


@dataclass(frozen=True)
class GitResult:
    """One finished git call. ``code`` is -1 when git could not be run at all.

    ``data`` holds stdout undecoded when the call asked for bytes (a patch
    must reach ``git apply`` byte for byte); ``stdout`` is then empty.
    """

    code: int
    stdout: str = ""
    stderr: str = ""
    data: bytes = b""

    @property
    def ok(self) -> bool:
        return self.code == 0

    def reason(self) -> str:
        """The last line git said about the failure, for an error message."""
        lines = [ln.strip() for ln in (self.stderr or self.stdout).splitlines() if ln.strip()]
        return lines[-1] if lines else f"git exited with code {self.code}"


def environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """A child's environment (no API keys) with the redirecting variables removed."""
    env = {k: v for k, v in subproc.child_env().items() if k not in _REDIRECTING_ENV}
    env["GIT_TERMINAL_PROMPT"] = "0"
    if extra:
        env.update(extra)
    return env


# The scopes a repository writes itself: ``.git/config``, a worktree's
# ``config.worktree``, and whatever those include.
_REPOSITORY_SCOPES = frozenset({"local", "worktree"})
# Commands that never read or write a file's content, so no filter can run and
# listing them would only double the process count of a status refresh.
_CONTENT_FREE = frozenset({
    "config", "rev-parse", "symbolic-ref", "update-ref", "branch", "for-each-ref",
})
# A handful is normal. Thousands would be a repository making the command line
# too long to start, which would be refused anyway; refuse it by name instead.
_MAX_FILTERS = 64


class FilterRefused(Exception):
    """The repository's filters cannot all be switched off, so git is not run."""


def filter_overrides(cwd: Path, env: dict[str, str] | None = None) -> tuple[str, ...]:
    """``-c`` options switching off every content filter the repository defines.

    An empty ``clean``/``smudge``/``process`` leaves the driver with nothing to
    run, and ``required=false`` keeps git from failing the file over that.
    Raises :class:`FilterRefused` when the list cannot be read, or names a
    filter an option cannot spell.
    """
    names = _repository_filter_names(cwd, environment() if env is None else env)
    if len(names) > _MAX_FILTERS:
        raise FilterRefused(f"the repository defines {len(names)} content filters")
    out: list[str] = []
    for name in sorted(names):
        # ``-c`` splits at the first "=", and a line break ends the option.
        if "=" in name or "\n" in name:
            raise FilterRefused(f"the repository defines a content filter named {name!r}")
        for key, value in (("clean", ""), ("smudge", ""), ("process", ""),
                           ("required", "false")):
            out += ["-c", f"filter.{name}.{key}={value}"]
    return tuple(out)


def _repository_filter_names(cwd: Path, env: dict[str, str]) -> set[str]:
    listing = _config_listing(cwd, env, "--show-scope")
    if listing is None:
        # Older than git 2.26: no scopes to tell the user's filters apart by,
        # so every filter is taken for the repository's.
        pairs = [("local", key) for key in _config_listing(cwd, env) or ()]
    else:
        pairs = list(zip(listing[::2], listing[1::2], strict=False))
    return {
        key[len("filter."):key.rindex(".")]
        for scope, key in pairs
        if scope in _REPOSITORY_SCOPES and key.count(".") >= 2
    }


def _config_listing(cwd: Path, env: dict[str, str], *options: str) -> list[str] | None:
    """The fields of ``git config -z --get-regexp ^filter\\.``; None when this
    git does not know one of ``options``. Raises :class:`FilterRefused`."""
    argv = ["git", "-C", str(cwd), "config", "-z", *options, "--name-only",
            "--get-regexp", r"^filter\."]
    try:
        proc = subproc.run(argv, capture_output=True, timeout=10, env=env,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        raise FilterRefused(f"could not list the repository's content filters: {exc}") from exc
    if proc.returncode == 1 and not proc.stdout:
        return []  # nothing matched
    if proc.returncode == 129 and options:
        return None  # usage error: an option this git predates
    if proc.returncode != 0:
        said = (proc.stderr or b"").decode("utf-8", errors="replace").strip().splitlines()
        raise FilterRefused(said[-1] if said else f"git config exited with {proc.returncode}")
    try:
        # Strictly: a name read with a replacement character would switch off
        # a filter that does not exist and leave the real one running.
        return [f for f in proc.stdout.decode("utf-8").split("\0") if f]
    except UnicodeDecodeError as exc:
        raise FilterRefused("the repository names a content filter that is not UTF-8") from exc


def _command(args: Sequence[str]) -> str:
    """The git subcommand in ``args``, past any ``-c key=value`` before it."""
    skip = False
    for arg in args:
        if skip:
            skip = False
        elif arg == "-c":
            skip = True
        elif not arg.startswith("-"):
            return arg
    return ""


def run(
    cwd: Path,
    *args: str,
    write: bool = False,
    timeout: float = 30.0,
    stdin: bytes | None = None,
    raw: bool = False,
    env: dict[str, str] | None = None,
) -> GitResult:
    """Run ``git -C cwd <BASE> [WRITE_SAFE] <filter overrides> args``. Never raises.

    ``write`` adds the options for commands that change the repository.
    ``raw`` keeps stdout as bytes (``GitResult.data``).
    """
    child_env = environment(env)
    filters: tuple[str, ...] = ()
    if _command(args) not in _CONTENT_FREE:
        try:
            filters = filter_overrides(cwd, child_env)
        except FilterRefused as exc:
            return GitResult(-1, "", f"git was not run: {exc}")
    argv = ["git", "-C", str(cwd), *BASE, *(WRITE_SAFE if write else ()), *filters, *args]
    try:
        proc = subproc.run(
            argv,
            input=stdin,
            capture_output=True,
            timeout=timeout,
            env=child_env,
            stdin=None if stdin is not None else subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return GitResult(-1, "", f"git {args[0] if args else ''} timed out after {timeout:g}s")
    except (OSError, subprocess.SubprocessError) as exc:
        return GitResult(-1, "", f"git could not be run: {exc}")
    out = proc.stdout or b""
    err = (proc.stderr or b"").decode("utf-8", errors="replace")
    if raw:
        return GitResult(proc.returncode, "", err, out)
    return GitResult(proc.returncode, out.decode("utf-8", errors="replace"), err)
