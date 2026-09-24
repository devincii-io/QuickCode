"""Reading a text file a person may have saved in any editor.

Windows editors -- Notepad, PowerShell's ``Set-Content`` and ``Out-File``,
Visual Studio -- save UTF-8 with a byte-order mark, or UTF-16. A plain UTF-8
read keeps the mark as U+FEFF at the start of the text, where it is invisible
and still wrong: a first line of ``\\ufeff---`` is not a frontmatter fence, and
a JSON document starting with one does not parse. A UTF-16 file does not decode
at all. The mark is what names the encoding here; without one the file is
UTF-8, strictly, because a guessed code page is a file read as something other
than what its author wrote.

One decoder for every file a person edits: ``settings.json`` and
``config.json`` (through ``jsonfile``), ``AGENTS.md`` and its siblings, and
authored plugin and agent files. The trust gate reads the same files to decide
what its grant covers, and a gate that decoded a file one way while a loader
decoded it another would bind a grant to something other than what runs.
"""

from __future__ import annotations

import codecs
import os
from pathlib import Path

# UTF-32 LE before UTF-16 LE: FF FE 00 00 starts with FF FE.
_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def decode(raw: bytes) -> str:
    """The text of a file's bytes, the mark removed. Raises ``UnicodeDecodeError``."""
    for bom, encoding in _BOMS:
        if raw.startswith(bom):
            return raw.decode(encoding)
    return raw.decode("utf-8")


def read_text(path: str | os.PathLike[str]) -> str:
    """``path``'s text, with line endings as ``Path.read_text`` gives them.

    Every ``\\r\\n`` and lone ``\\r`` becomes ``\\n``, as text mode does, so a
    reader moved here from ``read_text(encoding="utf-8")`` sees the same text
    for every file that has no mark. Raises ``OSError`` and
    ``UnicodeDecodeError``.
    """
    text = decode(Path(path).read_bytes())
    return text.replace("\r\n", "\n").replace("\r", "\n")
