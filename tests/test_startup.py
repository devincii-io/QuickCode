"""Nothing on the boot path waits on the network.

Opening the app used to await the provider's model catalog — 415 models, 3.2 s
measured — before uvicorn had bound its port, so the window could not appear
until an HTTP round trip to the provider came back. On a slow link, or a
captive-portal one, "QuickCode is slow to start" was really "QuickCode is
waiting for openrouter.ai".

These tests pin the contract rather than a stopwatch: the hub must not ask the
provider for anything while a project is opening, the launcher must be able to
start that fetch afterwards, and a conversation that opened without a catalog
must pick up its context length when the catalog finally lands.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from quickcode.config import Config
from quickcode.providers.base import ModelInfo
from quickcode.server.projects import ProjectHub
from tests.test_server import FakeProvider, make_env


class CountingProvider(FakeProvider):
    """A provider that records every catalog request and can stall on demand."""

    def __init__(self, *, block: asyncio.Event | None = None) -> None:
        super().__init__([])
        self.calls = 0
        self._block = block

    async def list_models(self):
        self.calls += 1
        if self._block is not None:
            await self._block.wait()
        return [ModelInfo(id="test/model", name="Test", context_length=100_000)]


async def _no_mcp(_cwd):
    return [], []


def _hub(tmp_path: Path, provider, **kw) -> ProjectHub:
    cfg = Config()
    cfg.last_model = "test/model"
    return ProjectHub(config=cfg, provider=provider, mcp_connect=_no_mcp, **kw)


async def test_opening_a_project_never_waits_on_the_provider(tmp_path: Path) -> None:
    # The catalog request blocks forever; opening must not care.
    provider = CountingProvider(block=asyncio.Event())
    hub = _hub(tmp_path, provider, defer_catalog=True)
    await asyncio.wait_for(
        hub.open(tmp_path, make_default=True, env=make_env(tmp_path)), timeout=5
    )
    assert provider.calls == 0, "the boot path asked the provider for its catalog"
    await hub.close()


async def test_the_launcher_starts_the_catalog_once_it_is_out_of_the_way(
    tmp_path: Path,
) -> None:
    provider = CountingProvider()
    hub = _hub(tmp_path, provider, defer_catalog=True)
    await hub.open(tmp_path, make_default=True, env=make_env(tmp_path))
    hub.warm_catalog()
    for _ in range(200):          # it runs as a task; give it a turn to finish
        if hub._models:
            break
        await asyncio.sleep(0.01)
    assert provider.calls == 1
    assert [m.id for m in hub._models or []] == ["test/model"]
    await hub.close()


async def test_a_second_project_reuses_the_catalog_it_already_paid_for(
    tmp_path: Path,
) -> None:
    provider = CountingProvider()
    hub = _hub(tmp_path, provider, defer_catalog=True)
    await hub.open(tmp_path, make_default=True, env=make_env(tmp_path))
    hub.warm_catalog()
    for _ in range(200):
        if hub._models:
            break
        await asyncio.sleep(0.01)
    second = tmp_path / "other"
    second.mkdir()
    manager = await hub.open(second, env=make_env(second))
    assert provider.calls == 1, "opening a second project paid for a second catalog"
    assert manager.model_info("test/model") is not None
    await hub.close()


async def test_a_conversation_learns_its_context_length_when_the_catalog_lands(
    tmp_path: Path,
) -> None:
    provider = CountingProvider()
    hub = _hub(tmp_path, provider, defer_catalog=True)
    manager = await hub.open(tmp_path, make_default=True, env=make_env(tmp_path))
    conv = manager.open()
    # Opened before the catalog: no context window is known yet, and the meter
    # says so rather than inventing one.
    assert conv.agent.context_length is None
    assert conv.agent.context_pct() is None

    manager.adopt_catalog(await provider.list_models())
    assert conv.agent.context_length == 100_000
    await hub.close()


async def test_closing_the_app_does_not_leave_the_catalog_fetch_running(
    tmp_path: Path,
) -> None:
    provider = CountingProvider(block=asyncio.Event())
    hub = _hub(tmp_path, provider, defer_catalog=True)
    await hub.open(tmp_path, make_default=True, env=make_env(tmp_path))
    hub.warm_catalog()
    await asyncio.sleep(0)        # let the task start and stall on the event
    await asyncio.wait_for(hub.close(), timeout=5)
    assert hub._models_task is None or hub._models_task.done()
