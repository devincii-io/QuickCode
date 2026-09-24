"""The native Anthropic Messages API behind QuickCode's ``Provider`` protocol.

Plain ``httpx`` rather than the SDK: the adapter needs one streaming POST and
one paginated GET, and the loop already speaks a wire-neutral event stream, so
an SDK would be a second dependency tree to ship for two requests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from quickcode.core.events import AgentEvent
from quickcode.providers.anthropic import sse
from quickcode.providers.anthropic.models import normalize_model, price_for, traits_for
from quickcode.providers.anthropic.retry import RetryPolicy, describe, retryable_status
from quickcode.providers.anthropic.stream import StreamError, StreamTranslator
from quickcode.providers.anthropic.wire import build_body
from quickcode.providers.base import ChatRequest, ModelInfo, ProviderError

log = logging.getLogger("quickcode.providers.anthropic")

DEFAULT_BASE_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"
FIRST_PARTY_HOST = "api.anthropic.com"

# Pings keep a healthy stream well inside the read timeout, and a long
# thinking pause still streams them.
TIMEOUT = httpx.Timeout(connect=15.0, read=300.0, write=60.0, pool=15.0)

_MODEL_PAGES = 10
_SIGNATURE_ERROR = re.compile(r"signature.*thinking|thinking.*signature", re.IGNORECASE | re.DOTALL)


def resolve_base_url(base_url: str | None) -> str:
    """Where the Messages API lives for this profile.

    A profile switched to this provider still carries OpenRouter's URL until
    someone edits it -- and sending an Anthropic key there would hand it to a
    third party. Anything on openrouter.ai therefore means "not configured
    for this provider" and falls back to the first-party endpoint.
    """
    url = (base_url or "").strip().rstrip("/")
    if not url or "openrouter.ai" in url:
        return DEFAULT_BASE_URL
    if url.endswith("/v1"):
        url = url[: -len("/v1")]
    return url


class AnthropicProvider:
    def __init__(
        self,
        base_url: str | None,
        api_key: str | None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        retry: RetryPolicy | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.base_url = resolve_base_url(base_url)
        self._api_key = api_key or ""
        self._transport = transport
        self._retry = retry or RetryPolicy()
        self._sleep = sleep
        # Per-model entries from GET /v1/models; they refine how thinking is
        # configured once the catalog has been fetched.
        self._models: dict[str, dict[str, Any]] = {}
        self._client: httpx.AsyncClient | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None

    @property
    def first_party(self) -> bool:
        return urlsplit(self.base_url).hostname == FIRST_PARTY_HOST

    def _http(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        if self._client is None or self._client.is_closed or self._client_loop is not loop:
            self._client = httpx.AsyncClient(transport=self._transport, timeout=TIMEOUT)
            self._client_loop = loop
        return self._client

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def stream_chat(self, req: ChatRequest) -> AsyncIterator[AgentEvent]:
        if not self._api_key:
            from quickcode.secrets import provider_key_env

            raise ProviderError(
                "No Anthropic API key is set. Save one in Settings or set "
                f"{provider_key_env('anthropic')}."
            )
        model = normalize_model(req.model)
        traits = traits_for(model, self._models.get(model))
        url = f"{self.base_url}/v1/messages"
        strip_reasoning = False
        attempt = 0

        while True:
            body = build_body(
                req,
                model=model,
                traits=traits,
                # Unbuffered tool input is a first-party field; a proxy or
                # gateway in front of the API may reject it.
                eager_tools=self.first_party,
                strip_reasoning=strip_reasoning,
            )
            # "replace": a lone surrogate from undecodable tool output must cost
            # one character, not the request.
            payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8", "replace"
            )
            translator = StreamTranslator(model)
            shown = False
            retry_after: str | None = None
            failure: str = ""
            try:
                async with self._http().stream(
                    "POST", url, content=payload, headers=self._headers()
                ) as resp:
                    if resp.status_code != 200:
                        raw = await resp.aread()
                        failure = describe(resp, raw)
                        if (
                            resp.status_code == 400
                            and not strip_reasoning
                            and _SIGNATURE_ERROR.search(failure)
                        ):
                            # History changed under a signed thinking block (a
                            # compaction, a model switch). The documented
                            # recovery: resend once without any of them.
                            log.info("replayed thinking refused; retrying without it")
                            strip_reasoning = True
                            continue
                        if not retryable_status(resp.status_code):
                            raise ProviderError(failure)
                        retry_after = resp.headers.get("retry-after")
                    else:
                        async for event in sse.events(resp.aiter_lines()):
                            for ev in translator.feed(event):
                                shown = True
                                yield ev
                        if not translator.finished:
                            raise StreamError("api_error", "the response stream ended early")
            except StreamError as exc:
                failure = f"Anthropic API stream error {exc.error_type}: {exc.message}"
                if shown or not exc.retryable:
                    async for ev in self._fail(translator, failure):
                        yield ev
            except httpx.TransportError as exc:
                failure = f"could not reach the Anthropic API: {exc.__class__.__name__}: {exc}"
                if shown:
                    async for ev in self._fail(translator, failure):
                        yield ev
            except sse.MalformedEvent as exc:
                async for ev in self._fail(translator, str(exc)):
                    yield ev

            if not failure:
                usage = translator.usage_event()
                if usage is not None:
                    yield usage
                yield translator.turn_done()
                return

            if attempt >= self._retry.max_retries:
                raise ProviderError(f"{failure} (gave up after {attempt + 1} attempts)")
            delay = self._retry.delay(attempt, retry_after)
            attempt += 1
            log.info("%s; retry %d in %.1fs", failure, attempt, delay)
            await self._sleep(delay)

    async def _fail(self, translator: StreamTranslator, message: str) -> AsyncIterator[AgentEvent]:
        """Surface a failure the caller has partly seen: what was spent first,
        so the ledger still counts it, then the error."""
        usage = translator.usage_event()
        if usage is not None:
            yield usage
        raise ProviderError(message)

    # ------------------------------------------------------------------
    # Model listing
    # ------------------------------------------------------------------

    async def list_models(self) -> list[ModelInfo]:
        if not self._api_key:
            return []
        out: list[ModelInfo] = []
        params: dict[str, Any] = {"limit": 1000}
        try:
            for _ in range(_MODEL_PAGES):
                resp = await self._http().get(
                    f"{self.base_url}/v1/models", params=params, headers=self._headers()
                )
                if resp.status_code != 200:
                    log.debug("model listing refused: %s", describe(resp, resp.content))
                    break
                page = resp.json()
                for entry in page.get("data") or []:
                    info = self._model_info(entry)
                    if info is not None:
                        out.append(info)
                if not page.get("has_more") or not page.get("last_id"):
                    break
                params = {"limit": 1000, "after_id": page["last_id"]}
        except Exception as exc:  # noqa: BLE001 - never crash on model listing
            log.debug("model listing failed: %s", exc)
        return out

    def _model_info(self, entry: Any) -> ModelInfo | None:
        if not isinstance(entry, dict) or not entry.get("id"):
            return None
        model_id = str(entry["id"])
        self._models[model_id] = entry
        context = entry.get("max_input_tokens")
        price = price_for(model_id)
        return ModelInfo(
            id=model_id,
            name=str(entry.get("display_name") or model_id),
            context_length=context if isinstance(context, int) and context > 0 else None,
            prompt_price=price.input if price else None,
            completion_price=price.output if price else None,
            supports_tools=True,
        )
