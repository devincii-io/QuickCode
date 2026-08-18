"""The project's ``.quickcode/`` directory, and keeping it out of git.

QuickCode writes a project's conversations into ``<project>/.quickcode/`` --
full transcripts under ``sessions/``, the task boards and offloaded subagent
reports that come out of the same turns. That directory sits inside the user's
own repository, so a habitual ``git add -A && git commit`` publishes every
prompt, every file the agent read and every line of command output. It is the
likeliest way this tool ever leaks something it should not.

The fix is a ``.gitignore`` *inside* ``.quickcode/``, written once when the
directory is created. Editing the project's own ``.gitignore`` would be the
obvious alternative and is the wrong one: that file is the user's, it is
committed, and a tool that silently rewrites it has earned every bit of the
distrust that follows. A ``.gitignore`` in a directory QuickCode created is
self-contained -- it covers exactly what QuickCode wrote, it travels with the
directory, and deleting the directory takes it with it.

Git is not required for any of this. A project that is not a repository just
ends up with one more inert text file.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_DIRNAME = ".quickcode"

# The paths that must never reach a commit, with the reasoning in the file
# itself -- someone finding this in a diff should not have to guess what it is
# protecting them from. Everything not listed here (settings.json, agents/,
# plugins/) is project configuration people legitimately share.
GITIGNORE_TEXT = """\
# Written by QuickCode when it created this directory. Without it, a routine
# `git add -A` in this project would commit your conversations.
#
#   sessions/            full transcripts -- every prompt, every file QuickCode
#                        read, every line of command output, anything pasted
#                        into the chat
#   tasks/               per-conversation task boards, written from those turns
#   artifacts/           subagent reports offloaded to disk: more transcript
#   plugins/.trash/      deleted plugin drafts, a local undo buffer
#   settings.local.json  this machine's permission grants, not the project's
#
# Deliberately not ignored: settings.json, agents/ and plugins/ are project
# configuration, meant to be reviewed and shared with everyone on the repo.
#
# QuickCode writes this file once and never rewrites it. It is yours now.

sessions/
tasks/
artifacts/
plugins/.trash/
settings.local.json
"""


def ensure_project_dir(root: Path | str) -> Path:
    """Create ``<root>/.quickcode/`` and, the first time, its ``.gitignore``.

    An existing ``.gitignore`` is never touched. Once the user has edited it --
    to share a session log deliberately, or to ignore more -- it is their file,
    and second-guessing that would be worse than the leak it prevents.
    """
    directory = Path(root) / PROJECT_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    gitignore = directory / ".gitignore"
    if not gitignore.exists():
        try:
            gitignore.write_text(GITIGNORE_TEXT, encoding="utf-8")
        except OSError:
            # Losing the guard is bad; losing the session because the guard
            # could not be written would be worse.
            pass
    return directory


def ensure_project_dir_for(path: Path | str) -> Path | None:
    """Same, for a writer that knows its own file but not the project root.

    Walks up from *path* to the ``.quickcode/`` it lives under. Returns
    ``None`` -- and does nothing -- if there is none, which is how a caller
    pointed somewhere else entirely stays harmless.
    """
    for parent in Path(path).parents:
        if parent.name == PROJECT_DIRNAME:
            return ensure_project_dir(parent.parent)
    return None
