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
    """Write the full report ``text`` to ``cwd/.quickcode/artifacts/{agent_id}.md``.

    Returns the path on success, or ``None`` on any OSError — never raises,
    so a write failure can never take down the parent's turn.
    """
    path = Path(cwd) / ".quickcode" / "artifacts" / f"{agent_id}.md"
    try:
        ensure_project_dir(cwd)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError:
        return None
    return path


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
