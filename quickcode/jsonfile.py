"""Reading a JSON file a person may have saved in any editor.

Windows editors -- Notepad, PowerShell's ``Set-Content`` and ``Out-File``,
Visual Studio -- save UTF-8 with a byte-order mark, or UTF-16. ``json.loads``
refuses the mark and a UTF-8 decode refuses UTF-16, so a settings file saved
that way used to be skipped as if it were empty. The mark is what names the
encoding here; without one the file is UTF-8, strictly, because a guessed code
page is a file read as something other than what its author wrote.

Every reader of ``settings.json`` and ``config.json`` goes through this one
decoder. The trust hash reads the project's settings files too, and a hash that
parsed a file one way while a loader parsed it another would bind a grant to
something other than what runs.
"""

from __future__ import annotations

import codecs
import json
import os
from pathlib import Path
from typing import Any

# UTF-32 LE before UTF-16 LE: FF FE 00 00 starts with FF FE.
_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def decode(raw: bytes) -> str:
    """The text of a JSON file. Raises ``UnicodeDecodeError``."""
    for bom, encoding in _BOMS:
        if raw.startswith(bom):
            return raw.decode(encoding)
    return raw.decode("utf-8")


def load(path: str | os.PathLike[str]) -> Any:
    """The parsed contents of ``path``.

    Raises ``OSError`` when it cannot be read and ``ValueError`` -- a
    ``UnicodeDecodeError`` or a ``json.JSONDecodeError`` -- when it is not JSON
    text, so a tolerant caller catches ``(OSError, ValueError)``.
    """
    return json.loads(decode(Path(path).read_bytes()))
