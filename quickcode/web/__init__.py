"""Outbound HTTP for the agent: what it may reach, and what comes back.

Three modules, in the order a fetch goes through them: ``ssrf`` decides whether
a URL leaves the machine, ``fetch`` performs the request with every redirect
re-checked and the body capped while streaming, ``markdown`` turns the page
into something worth spending context on.
"""

from quickcode.web.fetch import (
    DEFAULT_TIMEOUT_S,
    MAX_BYTES,
    MAX_REDIRECTS,
    FetchError,
    FetchOutcome,
    fetch_url,
    user_agent,
)
from quickcode.web.markdown import html_to_markdown
from quickcode.web.ssrf import BlockedURL, Target, classify_host, classify_ip, validate_url

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "MAX_BYTES",
    "MAX_REDIRECTS",
    "BlockedURL",
    "FetchError",
    "FetchOutcome",
    "Target",
    "classify_host",
    "classify_ip",
    "fetch_url",
    "html_to_markdown",
    "user_agent",
    "validate_url",
]
