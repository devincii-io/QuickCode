"""Text files as read, edit and write see them: bytes in, the same bytes out.

The file tools used to decode everything as UTF-8 with replacement and write it
back through Python's newline translation. Three ways that went wrong, all of
them on disk rather than in a transcript:

* a cp1252 or Latin-1 file had every non-ASCII byte turned into U+FFFD the
  first time *anything* in it was edited -- ``Größe`` became ``Gr��e``;
* a CRLF file had every line ending rewritten by an edit to one line, so a
  one-line change arrived as a whole-file diff;
* a UTF-16 file (what Windows PowerShell 5 writes by default) read as NULs
  between letters and could not be edited at all.

The rule now is that a file keeps the encoding, the byte-order mark and the
line-ending convention it had. The model only ever sees and writes ``\\n``;
the conversion at each end is this module's job.
"""

from __future__ import annotations

import codecs
import hashlib
import re
from dataclasses import dataclass

# How much of a file is examined for NUL bytes before calling it binary. Same
# window git and ripgrep use.
SNIFF_BYTES = 8192

# Checked in this order: the UTF-32 LE mark begins with the UTF-16 LE one.
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)

# What a text file that is not UTF-8 almost always is on the platform QuickCode
# ships for first. cp1252 leaves five bytes undefined, so Latin-1 -- which maps
# every byte -- is the floor: a file is never refused for its encoding, and
# whatever it decoded as, it encodes back to the same bytes.
_LEGACY_CODECS = ("cp1252", "latin-1")

_LONE_LF = re.compile(r"(?<!\r)\n")


class NotText(ValueError):
    """The file looks binary: there is a NUL byte and no byte-order mark."""


class Unencodable(ValueError):
    """The new text holds a character the file's encoding has no byte for."""

    def __init__(self, encoding: Encoding, char: str) -> None:
        self.encoding = encoding
        self.char = char
        super().__init__(
            f"the file is encoded as {encoding.label}, which has no way to store "
            f"{char!r} (U+{ord(char):04X}). Use characters that encoding has -- "
            "ASCII is always safe -- rather than changing the file's encoding."
        )


@dataclass(frozen=True)
class Encoding:
    codec: str
    bom: bytes = b""

    @property
    def label(self) -> str:
        return f"{self.codec} with a BOM" if self.bom else self.codec

    @property
    def is_default(self) -> bool:
        """Plain UTF-8: the one encoding nothing needs to be said about."""
        return self.codec == "utf-8" and not self.bom


UTF8 = Encoding("utf-8")


def detect(raw: bytes, *, complete: bool = True) -> Encoding:
    """The encoding ``raw`` is in. Raises :class:`NotText` for binary data.

    ``complete=False`` is for a leading sample of a larger file: a multi-byte
    UTF-8 sequence cut off by the end of the sample is not evidence of
    anything.
    """
    for bom, codec in _BOMS:
        if raw.startswith(bom):
            return Encoding(codec, bom)
    if b"\x00" in raw[:SNIFF_BYTES]:
        raise NotText("binary")
    try:
        codecs.getincrementaldecoder("utf-8")().decode(raw, final=complete)
        return UTF8
    except UnicodeDecodeError:
        pass
    for codec in _LEGACY_CODECS:
        try:
            raw.decode(codec)
        except UnicodeDecodeError:
            continue
        return Encoding(codec)
    return Encoding("latin-1")  # pragma: no cover - latin-1 decodes every byte


def decode(raw: bytes, encoding: Encoding, *, errors: str = "strict") -> str:
    return raw[len(encoding.bom):].decode(encoding.codec, errors=errors)


def encode(text: str, encoding: Encoding) -> bytes:
    """``text`` as the file stores it. Raises :class:`Unencodable`."""
    try:
        return encoding.bom + text.encode(encoding.codec)
    except UnicodeEncodeError as exc:
        raise Unencodable(encoding, exc.object[exc.start]) from None


def newline_of(text: str) -> str:
    """The file's line-ending convention: whichever of CRLF and LF is commoner.

    A tie goes to CRLF, because a file that has any CRLF in it was written by
    something that meant it, while a stray LF is usually a previous tool's
    damage.
    """
    crlf = text.count("\r\n")
    return "\r\n" if crlf and crlf >= text.count("\n") - crlf else "\n"


def with_newlines(text: str, newline: str) -> str:
    """``text`` with every bare LF written as ``newline``. Existing CRLFs stay."""
    return _LONE_LF.sub("\r\n", text) if newline == "\r\n" else text


def split_lines(text: str) -> list[str]:
    """Lines as ripgrep and every editor number them: split on LF only.

    ``str.splitlines`` also splits on form feed, vertical tab, U+0085 and the
    Unicode line and paragraph separators, so a file holding one of those was
    numbered differently here than by grep -- and an edit built from what read
    showed could not match the file.
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()
