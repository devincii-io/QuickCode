"""The one reader of ``---`` frontmatter, for everything that has to agree on it.

Two components read the same authored file for different reasons: the plugin
loader (``kernel/authoring/format.py``) to build the plugin, and the trust gate
(``security/trust.py``) to decide whether the file is a command tool its grant
must cover. They used to read it with two parsers, and where those disagreed --
a repeated ``kind:``, first copy to one, last copy to the other -- a trusted
project could add a tool no grant covered. So there is one parser, here, in a
leaf module both layers can import without the security layer reaching up into
the kernel.

The format: ``key: value`` lines between a first line of ``---`` and the next
line that is ``---``. Scalars only. An indented line continues the previous
key's value; a blank line ends that. A key written twice is recorded in
``duplicates`` rather than silently resolved -- which copy wins is exactly the
question two readers once answered differently, so callers refuse the file.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Frontmatter:
    meta: dict[str, str] = field(default_factory=dict)
    # 1-based line of each key (its last occurrence, as ``meta`` holds it).
    lines: dict[str, int] = field(default_factory=dict)
    # key -> every 1-based line that set it, for keys set more than once.
    duplicates: dict[str, tuple[int, ...]] = field(default_factory=dict)
    # Index of the first line after the closing ``---``; 0 when there is none.
    end: int = 0


def parse(text: str) -> Frontmatter:
    return split(text.splitlines())


def split(lines: list[str]) -> Frontmatter:
    """Never raises. No frontmatter, or an unterminated one, reads as none."""
    if not lines or lines[0].strip() != "---":
        return Frontmatter()
    close = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            close = i
            break
    if close < 0:
        # Unterminated frontmatter is a broken file, not a document whose body
        # happens to start with three dashes. Read nothing rather than half.
        return Frontmatter()

    meta: dict[str, str] = {}
    where: dict[str, list[int]] = {}
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
                where.setdefault(key, []).append(i + 1)
                last = key
                continue
        last = None
    return Frontmatter(
        meta=meta,
        lines={key: at[-1] for key, at in where.items()},
        duplicates={key: tuple(at) for key, at in where.items() if len(at) > 1},
        end=close + 1,
    )
