"""Running git without running the repository's own code.

A repository's configuration can name programs that git runs on its behalf: an
fsmonitor hook on every status, external diff and textconv drivers on every
diff, hooks on commit, checkout and every ref update, a signing program on
commit. QuickCode runs git the moment a project opens (the git panel,
``server/gitinfo.py``) and on a subagent's behalf (worktree isolation,
``subagents/worktree.py``) -- before anyone has necessarily trusted the
project -- so none of those may run. The options that say so live here, once,
so the two callers cannot drift apart about what "safe" means.

Content filters (``filter.<name>.clean`` / ``smudge``, which is how Git LFS
works) are left alone: disabling them would check an LFS repository out as
pointer files, and the panel's ``git status`` runs clean filters already.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from quickcode import subproc

# Options every call carries. No fsmonitor hook, and ``--literal-pathspecs``
# because a path proved to be inside the project would otherwise still be read
# as pathspec magic -- ``:(top)``/``:/`` name the repository root, which for a
# project nested in a larger repository is above it. And no optional locks, so
# a status refresh never leaves the user's own ``git commit`` failing on a held
# ``index.lock``.
BASE = (
    "--literal-pathspecs", "--no-optional-locks",
    "-c", "core.quotepath=off", "-c", "core.fsmonitor=false",
)
# External diff and textconv drivers stay out of every diff.
DIFF_SAFE = ("--no-ext-diff", "--no-textconv")
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
    """The process environment with the redirecting variables removed."""
    env = {k: v for k, v in os.environ.items() if k not in _REDIRECTING_ENV}
    env["GIT_TERMINAL_PROMPT"] = "0"
    if extra:
        env.update(extra)
    return env


def run(
    cwd: Path,
    *args: str,
    write: bool = False,
    timeout: float = 30.0,
    stdin: bytes | None = None,
    raw: bool = False,
    env: dict[str, str] | None = None,
) -> GitResult:
    """Run ``git -C cwd <BASE> [WRITE_SAFE] args``. Never raises.

    ``write`` adds the options for commands that change the repository.
    ``raw`` keeps stdout as bytes (``GitResult.data``).
    """
    argv = ["git", "-C", str(cwd), *BASE, *(WRITE_SAFE if write else ()), *args]
    try:
        proc = subproc.run(
            argv,
            input=stdin,
            capture_output=True,
            timeout=timeout,
            env=environment(env),
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
