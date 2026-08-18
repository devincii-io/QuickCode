"""The one parser for markdown-with-frontmatter, for every authored kind.

``subagents/definitions.py`` has read ``---`` frontmatter since agents became
authorable, and it worked. This module is that reader generalised rather than a
second one written beside it: ``_split_frontmatter`` now delegates here, so the
agent loader and the plugin loader cannot drift apart.

Three things come out of a document and nothing else does:

``meta``    the frontmatter, ``key: value``, scalars and inline lists only.
            An indented continuation line appends to the previous key, which is
            how a two-line ``description:`` stays one value.
``body``    everything after the closing ``---``, verbatim.
``blocks``  fenced blocks whose info string carries a *tag* -- ```` ```json
            params ````, ```` ```json argv ````, ```` ```text stdin ````. The
            tag is the second word; a fence with only a language is ordinary
            prose and is left in the body.

No YAML. The format has no use for anchors, nested maps or multi-document
streams, and a parser that could express them would invite files this loader
would then have to refuse. Line numbers are recorded because a problem that
cannot point at a line makes the user hunt for their own typo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*(.*)$")


@dataclass(frozen=True)
class Block:
    """One tagged fenced block: ```` ```<lang> <tag> ````."""

    tag: str
    lang: str
    text: str
    line: int  # 1-based line of the opening fence


@dataclass(frozen=True)
class Document:
    meta: dict[str, str] = field(default_factory=dict)
    meta_lines: dict[str, int] = field(default_factory=dict)
    body: str = ""
    body_line: int = 1
    blocks: dict[str, Block] = field(default_factory=dict)
    # The body with every tagged block removed: a tool's long description, an
    # agent's system prompt, a section's text.
    prose: str = ""

    def line_of(self, key: str) -> int:
        return self.meta_lines.get(key, 0)


def parse_document(text: str) -> Document:
    """Never raises. A file that is not in this shape parses as all-body."""
    lines = text.splitlines()
    meta, meta_lines, start = _frontmatter(lines)
    body_lines = lines[start:]
    body = "\n".join(body_lines)
    blocks, prose = _blocks(body_lines, start)
    return Document(
        meta=meta,
        meta_lines=meta_lines,
        body=body,
        body_line=start + 1,
        blocks=blocks,
        prose=prose,
    )


def _frontmatter(lines: list[str]) -> tuple[dict[str, str], dict[str, int], int]:
    if not lines or lines[0].strip() != "---":
        return {}, {}, 0
    close = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            close = i
            break
    if close < 0:
        # Unterminated frontmatter is a broken file, not a document whose body
        # happens to start with three dashes. Read nothing rather than half.
        return {}, {}, 0

    meta: dict[str, str] = {}
    meta_lines: dict[str, int] = {}
    last: str | None = None
    for i in range(1, close):
        raw = lines[i]
        if not raw.strip():
            last = None
            continue
        if raw[:1] in (" ", "\t") and last is not None:
            meta[last] = f"{meta[last]} {raw.strip()}".strip()
            continue
        if ":" in raw:
            key, _, value = raw.partition(":")
            key = key.strip()
            if key:
                meta[key] = value.strip()
                meta_lines[key] = i + 1
                last = key
                continue
        last = None
    return meta, meta_lines, close + 1


def _blocks(body_lines: list[str], offset: int) -> tuple[dict[str, Block], str]:
    """Tagged fenced blocks, plus the body with those blocks removed."""
    blocks: dict[str, Block] = {}
    kept: list[str] = []
    i = 0
    while i < len(body_lines):
        m = _FENCE_RE.match(body_lines[i])
        if m is None:
            kept.append(body_lines[i])
            i += 1
            continue
        indent, fence, info = m.group(1), m.group(2), m.group(3).strip()
        parts = info.split()
        tag = parts[1] if len(parts) > 1 else ""
        lang = parts[0] if parts else ""
        opened = i
        i += 1
        payload: list[str] = []
        closed = False
        while i < len(body_lines):
            close = _FENCE_RE.match(body_lines[i])
            if close is not None and close.group(2)[0] == fence[0] \
                    and len(close.group(2)) >= len(fence) and not close.group(3).strip():
                closed = True
                i += 1
                break
            payload.append(body_lines[i])
            i += 1
        if tag and tag not in blocks:
            blocks[tag] = Block(
                tag=tag, lang=lang, text="\n".join(payload),
                line=offset + opened + 1,
            )
            continue  # tagged blocks leave the prose
        # Untagged (or a second block with the same tag): keep it in the prose
        # exactly as written, fences included.
        kept.append(f"{indent}{fence}{(' ' + info) if info else ''}")
        kept.extend(payload)
        if closed:
            kept.append(f"{indent}{fence}")
    return blocks, "\n".join(kept).strip()


def parse_list(raw: str) -> list[str]:
    """``[read, glob, grep]`` or ``read, glob`` -> a list of strings."""
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [item.strip().strip("'\"") for item in raw.split(",") if item.strip()]


def parse_bool(raw: str, default: bool = False) -> bool:
    text = (raw or "").strip().lower()
    if not text:
        return default
    if text in ("true", "yes", "on", "1"):
        return True
    if text in ("false", "no", "off", "0"):
        return False
    return default


def is_bool(raw: str) -> bool:
    return (raw or "").strip().lower() in (
        "true", "yes", "on", "1", "false", "no", "off", "0",
    )
