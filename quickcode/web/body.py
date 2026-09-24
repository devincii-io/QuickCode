"""A response body, from bytes on the wire to text worth reading.

Three steps, each with a way to go wrong that ``fetch.py`` used to leave open:

1. **Inflate, bounded.** httpx inflates each network chunk whole, so the byte
   cap saw 64 KB of gzip only after it had become 64 MB of zeros. The body is
   read raw here and inflated with an output limit, so the cap holds for the
   bytes produced, not the bytes received. Only gzip and deflate are
   accepted -- the two zlib can inflate incrementally with a limit -- and are
   the only two advertised.
2. **Charset.** BOM, then the ``Content-Type`` charset, then an HTML
   ``<meta charset>``, then UTF-8: the HTML spec's order.
3. **Binary sniff.** A NUL in the first 8 KB of something not declared as
   UTF-16/32 is binary whatever the server called it.
"""

from __future__ import annotations

import codecs
import re
import zlib

import httpx

ACCEPT_ENCODING = "gzip, deflate"

_WBITS = {
    "gzip": 16 + zlib.MAX_WBITS,
    "x-gzip": 16 + zlib.MAX_WBITS,
    "deflate": zlib.MAX_WBITS,
}
_BOM_CODECS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9._:-]+)""", re.I)
# Where a page's <meta charset> may be; the HTML spec's prescan reads 1024.
_META_SCAN_BYTES = 2048
_BINARY_SNIFF_BYTES = 8192


class UnsupportedEncoding(ValueError):
    """A ``Content-Encoding`` this module will not inflate."""


class _Inflater:
    def __init__(self, encoding: str) -> None:
        self._raw_deflate = encoding == "deflate"
        self._z = zlib.decompressobj(_WBITS[encoding])
        self._started = False

    def feed(self, data: bytes, limit: int) -> bytes:
        """Up to ``limit + 1`` bytes of output: one past the limit is how the
        caller learns the body goes on."""
        out = bytearray()
        while data and len(out) <= limit:
            try:
                out += self._z.decompress(data, limit + 1 - len(out))
            except zlib.error:
                # "deflate" is meant to be zlib-wrapped and is sometimes sent
                # raw; the first chunk says which.
                if not (self._raw_deflate and not self._started):
                    raise
                self._z = zlib.decompressobj(-zlib.MAX_WBITS)
                self._raw_deflate = False
                continue
            self._started = True
            data = self._z.unconsumed_tail
        return bytes(out)


async def read_capped(response: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
    """The body, inflated, stopping at ``max_bytes``. Returns (bytes, truncated).

    Raises :class:`UnsupportedEncoding` for a content coding other than gzip
    or deflate, and ``zlib.error`` for a corrupt one.
    """
    if response.is_stream_consumed:
        # Already read into memory by whoever built it -- a response made in
        # a test, never one off the network. Its content is already decoded.
        data = response.content
        return data[:max_bytes], len(data) > max_bytes

    coding = response.headers.get("content-encoding", "").strip().lower()
    inflater = None
    if coding and coding != "identity":
        if coding not in _WBITS:
            raise UnsupportedEncoding(coding)
        inflater = _Inflater(coding)

    chunks: list[bytes] = []
    total = 0
    async for raw in response.aiter_raw():
        chunk = inflater.feed(raw, max_bytes - total) if inflater else raw
        total += len(chunk)
        if total > max_bytes:
            chunks.append(chunk[: max_bytes - (total - len(chunk))])
            return b"".join(chunks), True
        chunks.append(chunk)
    return b"".join(chunks), False


def charset_of(raw: bytes, declared: str | None, content_type: str) -> str:
    """BOM, then the header's charset, then ``<meta charset>``, then UTF-8.

    Without the ``<meta>`` step a legacy page that declares its code page only
    in its markup -- common, since a header is server configuration and a meta
    tag is just the file -- came back with every non-ASCII character replaced.
    """
    for bom, codec in _BOM_CODECS:
        if raw.startswith(bom):
            return codec
    candidates = [declared]
    if "html" in content_type.lower() or not content_type:
        match = _META_CHARSET.search(raw[:_META_SCAN_BYTES])
        if match:
            candidates.append(match.group(1).decode("ascii", "replace"))
    for name in candidates:
        if not name:
            continue
        try:
            return codecs.lookup(name).name
        except LookupError:
            continue
    return "utf-8"


def looks_binary(raw: bytes, charset: str) -> bool:
    """A NUL in the first 8 KB of something not declared as UTF-16/32 -- the
    test git and ripgrep use -- for a server that labels a zip ``text/plain``
    or labels nothing at all."""
    if charset.startswith(("utf-16", "utf-32", "utf_16", "utf_32")):
        return False
    return b"\x00" in raw[:_BINARY_SNIFF_BYTES]
