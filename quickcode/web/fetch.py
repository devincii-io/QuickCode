"""One guarded HTTP GET: redirects stepped through, body capped while reading.

``httpx`` will follow redirects for you, and that is exactly what this module
does not do. ``follow_redirects=True`` validates the URL you handed it and then
follows a ``302`` to wherever it likes -- so a public URL that redirects to
``http://127.0.0.1:8765/api/sessions`` is a validated fetch of the agent's own
control plane. Every hop is therefore a fresh :func:`validate_url`.

The body is capped **while streaming**, not after: a fetch tool that buffers
whatever arrives and truncates at the end is a fetch tool that can be handed a
multi-gigabyte response. ``Content-Length`` is checked first when it is there,
and the read is aborted the moment the cap is crossed either way.

Nothing sensitive can leak outward, because nothing sensitive is ever sent: the
tool takes no headers from the caller, so there is no ``Authorization`` to
forward, and the cookie jar is emptied after every hop, so a ``Set-Cookie`` on a
redirect cannot be replayed to whatever it redirected to. The requirement to
strip credentials before following a redirect to another host is met by never
having any to strip.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from urllib.parse import urljoin

import httpx

from quickcode.web.ssrf import BlockedURL, Resolver, Target, validate_url

MAX_BYTES = 4_000_000
MAX_REDIRECTS = 5
DEFAULT_TIMEOUT_S = 30.0
MAX_TIMEOUT_S = 120.0

# Content types worth handing a language model. Anything else is refused with
# its type named, which is more useful than 400 KB of decoded PNG.
TEXTUAL_PREFIXES = ("text/",)
TEXTUAL_TYPES = {
    "application/json",
    "application/ld+json",
    "application/xml",
    "application/xhtml+xml",
    "application/rss+xml",
    "application/atom+xml",
    "application/javascript",
    "application/x-ndjson",
    "application/yaml",
    "application/x-yaml",
}


def user_agent() -> str:
    """A truthful UA: what this is, which version, and where to complain."""
    try:
        from importlib.metadata import version

        release = version("quickcode")
    except Exception:  # noqa: BLE001 - running from a source tree
        release = "dev"
    return (
        f"QuickCode/{release} (+https://github.com/devincii-io/QuickCode; "
        "web_fetch tool; automated request on a user's behalf)"
    )


class FetchError(RuntimeError):
    """The fetch failed for a reason the model should read and act on."""


@dataclass
class FetchOutcome:
    url: str
    final_url: str
    status: int
    content_type: str
    body: str
    bytes_read: int
    truncated: bool = False
    redirects: list[str] = field(default_factory=list)


def _is_textual(content_type: str) -> bool:
    kind = content_type.split(";", 1)[0].strip().lower()
    if not kind:
        return True  # no type declared: assume text and let decoding decide
    return kind.startswith(TEXTUAL_PREFIXES) or kind in TEXTUAL_TYPES


def build_request(target: Target, *, headers: dict[str, str] | None = None) -> httpx.Request:
    """A GET aimed at the validated address, still addressed to the name.

    The URL carries the IP so the connection cannot re-resolve; the ``Host``
    header and the ``sni_hostname`` extension carry the name, so virtual
    hosting still works and the TLS certificate is still verified against the
    hostname rather than against an address it will never match.
    """
    merged = {
        "User-Agent": user_agent(),
        "Accept": "text/html,application/xhtml+xml,text/plain,application/json;q=0.9,*/*;q=0.5",
        "Accept-Language": "en,*;q=0.5",
        "Host": target.header_host,
        **(headers or {}),
    }
    return httpx.Request(
        "GET",
        target.pinned_url(),
        headers=merged,
        extensions={"sni_hostname": target.host},
    )


async def _read_capped(response: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    total = 0
    truncated = False
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            chunks.append(chunk[: max_bytes - (total - len(chunk))])
            truncated = True
            break
        chunks.append(chunk)
    return b"".join(chunks), truncated


async def fetch_url(
    url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    resolve: Resolver | None = None,
    max_bytes: int = MAX_BYTES,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_redirects: int = MAX_REDIRECTS,
) -> FetchOutcome:
    """Fetch one URL, validating every hop. Raises :class:`FetchError`."""
    timeout_s = max(1.0, min(float(timeout_s), MAX_TIMEOUT_S))
    try:
        async with asyncio.timeout(timeout_s):
            return await _fetch(url, transport, resolve, max_bytes, max_redirects)
    except TimeoutError as exc:
        raise FetchError(
            f"timed out after {timeout_s:.0f}s fetching {url} (including redirects)."
        ) from exc


async def _fetch(
    url: str,
    transport: httpx.AsyncBaseTransport | None,
    resolve: Resolver | None,
    max_bytes: int,
    max_redirects: int,
) -> FetchOutcome:
    redirects: list[str] = []
    current = url

    async with httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        timeout=httpx.Timeout(10.0, read=20.0),
    ) as client:
        for _ in range(max_redirects + 1):
            try:
                target = await validate_url(current, resolve=resolve)
            except BlockedURL as exc:
                where = " (after a redirect)" if redirects else ""
                raise FetchError(f"{exc}{where}") from exc

            request = build_request(target)
            try:
                response = await client.send(request, stream=True)
            except httpx.HTTPError as exc:
                raise FetchError(
                    f"could not fetch {target.host}: {type(exc).__name__}."
                ) from exc

            # Requests are built by hand and dispatched with client.send, which
            # never attaches stored cookies -- but the client does collect them
            # from responses, so drop them rather than rely on that asymmetry.
            client.cookies.clear()

            location = response.headers.get("location", "")
            if response.is_redirect and location:
                await response.aclose()
                redirects.append(current)
                current = urljoin(current, location)
                continue

            try:
                content_type = response.headers.get("content-type", "")
                if not _is_textual(content_type):
                    raise FetchError(
                        f"{target.host} answered with {content_type.split(';')[0]}, "
                        "which is not text. web_fetch reads text, HTML and JSON only."
                    )

                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise FetchError(
                        f"{target.host} declared a {int(declared):,}-byte body, over the "
                        f"{max_bytes:,}-byte limit. Nothing was downloaded."
                    )

                if response.status_code >= 400:
                    raise FetchError(
                        f"{target.host} returned HTTP {response.status_code} "
                        f"({response.reason_phrase or 'error'})."
                    )

                raw, truncated = await _read_capped(response, max_bytes)
            finally:
                await response.aclose()

            encoding = response.charset_encoding or "utf-8"
            try:
                body = raw.decode(encoding, errors="replace")
            except LookupError:
                body = raw.decode("utf-8", errors="replace")

            return FetchOutcome(
                url=url,
                final_url=target.url,
                status=response.status_code,
                content_type=content_type,
                body=body,
                bytes_read=len(raw),
                truncated=truncated,
                redirects=redirects,
            )

    raise FetchError(
        f"gave up after {max_redirects} redirects starting at {url}. "
        "The last hop was still redirecting."
    )
