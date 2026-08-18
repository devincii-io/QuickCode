"""What ``web_fetch`` is allowed to connect to, and how it is made to stick.

QuickCode's own API listens on 127.0.0.1 behind a token, and the machine it
runs on is usually on a LAN with printers, routers, NAS boxes and cloud
metadata services on it. A fetch tool is a request the *model* composes, and
the model composes it from text it read on a web page, in an issue comment, in
a file somebody else wrote. So the URL is attacker-reachable input, and the
question this module answers is not "is this a URL" but "does this URL leave
the machine".

Four rules, in the order they are applied:

1. **Scheme.** http and https only. ``file:``, ``ftp:``, ``data:``, ``gopher:``
   and everything else are refused before anything is parsed further.
2. **Name.** ``localhost``, ``*.local``, ``*.internal``, ``*.lan``,
   ``*.home.arpa`` and any bare hostname with no dot are refused. A bare name
   resolves through the machine's own search domains, which is how an intranet
   host gets reached without ever looking private.
3. **Address.** Every address the name resolves to is classified, and one bad
   address refuses the whole name -- not "pick a good one". A host answering
   with both a public and a loopback address is not a host with a public
   address, it is an attack.
4. **Pinning.** The request is then sent to the address that was checked, with
   the original name in the Host header and in the TLS SNI, so certificate
   verification still happens against the name. This is what closes the gap
   between checking and connecting: without it, the name is resolved twice and
   the second answer -- the one that is actually connected to -- was never
   checked. That is DNS rebinding, and it is the standard way past a validator
   that only validates.

Redirects are the other standard bypass and they are not handled here: they are
handled by ``fetch.py`` running this module again on every single hop.

Known gaps, stated rather than papered over:

* A **public host that proxies inward** (an open proxy, an SSRF-vulnerable
  service, a URL shortener resolving server-side) is indistinguishable from a
  legitimate public host at this layer. Nothing on the client side can see it.
* **HTTP/2 and connection reuse.** Not a live gap -- ``fetch.py`` opens a client
  per fetch and httpx speaks HTTP/1.1 unless the h2 extra is installed -- but a
  pooled connection keyed by hostname rather than by the pinned address would
  reintroduce the rebinding window, so the pinning and the pooling have to stay
  the way they are.
* **IPv6 transition addresses.** 6to4, Teredo and NAT64-style embeddings are
  unwrapped and their embedded IPv4 checked, but an exotic future embedding
  would be classified on its outer form only.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

ALLOWED_SCHEMES = ("http", "https")

# Suffixes that mean "not the public internet" by convention or by RFC. A name
# ending in one of these is refused without asking DNS, because on many
# machines DNS would answer helpfully.
BLOCKED_SUFFIXES = (
    ".local",       # mDNS
    ".localhost",
    ".internal",    # common private convention, and GCP's metadata domain
    ".intranet",
    ".lan",
    ".home.arpa",   # RFC 8375
    ".corp",
    ".private",
)

BLOCKED_NAMES = ("localhost",)

Resolver = Callable[[str, int], Awaitable[list[str]]]


class BlockedURL(ValueError):
    """Refused before a single packet was sent. The message says why."""


@dataclass(frozen=True)
class Target:
    """A URL that passed every check, plus the address it may be sent to."""

    url: str          # the URL as written (normalized)
    scheme: str
    host: str         # hostname as written, lowercased -- for SNI and Host
    port: int
    ip: str           # the one validated address this request may connect to
    header_host: str  # Host header value, with the port when it is non-default

    @property
    def is_ipv6(self) -> bool:
        return ":" in self.ip

    def pinned_url(self) -> str:
        """The same request, addressed to the checked IP instead of the name."""
        parts = urlsplit(self.url)
        literal = f"[{self.ip}]" if self.is_ipv6 else self.ip
        netloc = f"{literal}:{self.port}"
        return urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))


# --------------------------------------------------------------------------
# Address classification
# --------------------------------------------------------------------------


def classify_ip(raw: str) -> str:
    """"" if this address is on the public internet, else why it is refused."""
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return f"{raw!r} is not a valid IP address"

    if isinstance(ip, ipaddress.IPv6Address):
        # An IPv6 address can carry an IPv4 one inside it. ::ffff:127.0.0.1 is
        # loopback however it is spelled, and some stacks will happily connect.
        embedded = ip.ipv4_mapped or ip.sixtofour
        if embedded is None and ip.teredo is not None:
            embedded = ip.teredo[1]
        if embedded is not None:
            inner = classify_ip(str(embedded))
            if inner:
                return f"{raw} embeds the IPv4 address {embedded}, which {inner}"

    if ip.is_unspecified:
        return "is the unspecified address (0.0.0.0 / ::), which means 'this host'"
    if ip.is_loopback:
        return "is a loopback address — that is this machine, where QuickCode's own API listens"
    if ip.is_link_local:
        return "is link-local (169.254/16, fe80::/10) — cloud metadata lives there"
    if ip.is_private:
        # Covers RFC1918, unique-local fc00::/7, and the rest of the private
        # ranges ipaddress knows about.
        return "is a private address — that is the local network, not the internet"
    if ip.is_multicast:
        return "is a multicast address"
    if ip.is_reserved:
        return "is in a reserved range"
    if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"):
        return "is carrier-grade NAT space (100.64/10)"
    return ""


def classify_host(host: str) -> str:
    """"" if this hostname may be resolved at all, else why it is refused."""
    name = (host or "").strip().rstrip(".").lower()
    if not name:
        return "the URL has no host"

    # An IP literal skips the name rules and goes straight to classification.
    try:
        ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        return classify_ip(name)

    if name in BLOCKED_NAMES:
        return f"{name!r} is this machine"
    for suffix in BLOCKED_SUFFIXES:
        if name.endswith(suffix):
            return f"{name!r} is a {suffix} name, which is not on the public internet"
    if "." not in name:
        return (
            f"{name!r} is a bare hostname with no dot — it would resolve through "
            "this machine's search domains, which is how intranet hosts get reached"
        )
    return ""


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


async def default_resolver(host: str, port: int) -> list[str]:
    """Every address ``host`` answers with, IPv4 and IPv6, in DNS order."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    out: list[str] = []
    for info in infos:
        address = info[4][0]
        if address not in out:
            out.append(address)
    return out


async def validate_url(
    url: str,
    *,
    resolve: Resolver | None = None,
) -> Target:
    """Run every check and return the one address this URL may be sent to.

    Raises :class:`BlockedURL` with a sentence the model can act on. It never
    returns a partially checked target: if any resolved address is not public,
    the whole name is refused.
    """
    raw = (url or "").strip()
    if not raw:
        raise BlockedURL("no URL was given.")

    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if not scheme:
        raise BlockedURL(
            f"{raw!r} has no scheme. Give a full URL starting with http:// or https://."
        )
    if scheme not in ALLOWED_SCHEMES:
        raise BlockedURL(
            f"the {scheme}: scheme is not fetchable. web_fetch speaks http and https "
            "only — use read for local files."
        )
    if parts.username or parts.password:
        raise BlockedURL(
            "the URL carries credentials in it (user:password@host). Refused: those "
            "would be sent, logged and followed through redirects."
        )

    host = (parts.hostname or "").strip().rstrip(".").lower()
    reason = classify_host(host)
    if reason:
        raise BlockedURL(f"refusing to fetch {host or raw!r}: it {reason}.")

    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:  # non-numeric port in the authority
        raise BlockedURL(f"{raw!r} has an invalid port.") from exc

    resolver = resolve or default_resolver
    try:
        addresses = await resolver(host, port)
    except OSError as exc:
        raise BlockedURL(f"could not resolve {host!r}: {exc}.") from exc
    if not addresses:
        raise BlockedURL(f"{host!r} does not resolve to any address.")

    for address in addresses:
        bad = classify_ip(address)
        if bad:
            raise BlockedURL(
                f"refusing to fetch {host!r}: it resolves to {address}, which {bad}."
            )

    default_port = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    return Target(
        url=urlunsplit((scheme, parts.netloc, parts.path or "/", parts.query, "")),
        scheme=scheme,
        host=host,
        port=port,
        ip=addresses[0],
        header_host=host if default_port else f"{host}:{port}",
    )
