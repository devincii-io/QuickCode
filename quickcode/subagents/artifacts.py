"""Offload large subagent reports to disk, passing the parent a short head plus
a file reference instead of the full text (docs/AGENTS.md §1 "Artifacts to disk,
references in reports").

A subagent's full final report normally flows back through the parent's
context verbatim. For large reports (long logs, big dumps, generated code)
that floods context for no benefit — the parent usually only needs a summary
and can read the file if it needs the rest.
"""

from __future__ import annotations

from pathlib import Path

from quickcode.workspace import ensure_project_dir

ARTIFACT_CHAR_LIMIT = 1500
ARTIFACT_HEAD_LINES = 40


def write_artifact(cwd: Path, agent_id: str, text: str) -> Path | None:
    """Write the full report ``text`` under ``cwd/.quickcode/artifacts/``.

    Returns the path actually written, or ``None`` on any OSError — never
    raises, so a write failure can never take down the parent's turn.

    The name is *claimed*, not assumed. Agent ids come from a counter that
    restarts at 1 in every conversation, so ``explore-1.md`` is the first
    offloaded report of every session that ever ran one — and this used to
    write it with a plain ``write_text``, so opening a new conversation and
    fanning out silently destroyed the previous session's report, while its
    transcript went on pointing at the file and now described someone else's
    work. Three reports in this repository were already lost that way. An
    existing file is never overwritten; the next free ``-2``, ``-3`` … is
    taken instead, and the caller quotes the path it gets back.
    """
    directory = Path(cwd) / ".quickcode" / "artifacts"
    try:
        ensure_project_dir(cwd)
        directory.mkdir(parents=True, exist_ok=True)
        for suffix in ("", *(f"-{n}" for n in range(2, 1000))):
            path = directory / f"{agent_id}{suffix}.md"
            try:
                # x: create-or-fail, so two subagents racing for the same name
                # cannot both believe they got it.
                with path.open("x", encoding="utf-8") as handle:
                    handle.write(text)
            except FileExistsError:
                continue
            return path
        return None
    except OSError:
        return None


def maybe_offload(cwd: Path, agent_id: str, report: str) -> str:
    """Return ``report`` unchanged if small; otherwise write it to disk and
    return a trimmed head plus a pointer to the full file.

    If the write fails, the original full report is returned unchanged —
    offloading is a context-size optimization, never a data-loss risk.
    """
    lines = report.splitlines()
    if len(report) <= ARTIFACT_CHAR_LIMIT and len(lines) <= ARTIFACT_HEAD_LINES:
        return report

    path = write_artifact(cwd, agent_id, report)
    if path is None:
        return report

    head = "\n".join(lines[:ARTIFACT_HEAD_LINES])[:ARTIFACT_CHAR_LIMIT]
    marker = (
        f"\n\n[full report ({len(report)} chars) written to {path}; "
        "read that file for the rest]"
    )
    return head + marker
