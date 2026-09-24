"""Reading a JSON file a person may have saved in any editor.

Windows editors save UTF-8 with a byte-order mark, or UTF-16; ``json.loads``
refuses the mark and a UTF-8 decode refuses UTF-16, so a settings file saved
that way used to be skipped as if it were empty. The bytes are decoded by
``quickcode/textio.py``, the one decoder for every file a person edits.

Every reader of ``settings.json`` and ``config.json`` goes through this. The
trust hash reads the project's settings files too, and a hash that parsed a
file one way while a loader parsed it another would bind a grant to something
other than what runs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from quickcode.textio import decode

__all__ = ["decode", "load"]


def load(path: str | os.PathLike[str]) -> Any:
    """The parsed contents of ``path``.

    Raises ``OSError`` when it cannot be read and ``ValueError`` -- a
    ``UnicodeDecodeError`` or a ``json.JSONDecodeError`` -- when it is not JSON
    text, so a tolerant caller catches ``(OSError, ValueError)``.
    """
    return json.loads(decode(Path(path).read_bytes()))
