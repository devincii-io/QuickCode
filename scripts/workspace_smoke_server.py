"""Disposable UI review server. No real provider, credentials, or project files.

Run through uv with --no-sync, then open the printed URL. Ctrl+C stops it.

    workspace_smoke_server.py [port] [--jobs]

``--jobs`` makes the preview agent answer a message by starting a background
job (``bash`` with ``run_in_background``) that prints a coloured tick five times
a second until it is killed -- the Jobs tab's subject, for
``scripts/smoke_jobs.js``. The permission prompt for it is the real one.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

TICKER = """\
import itertools, time
for i in itertools.count(1):
    print(f"\\x1b[32mtick\\x1b[0m {i} \\x1b[2m{'#' * (i % 20)}\\x1b[0m", flush=True)
    time.sleep(0.2)
"""


def main() -> None:
    argv = sys.argv[1:]
    jobs = "--jobs" in argv
    positional = [a for a in argv if not a.startswith("--")]
    with tempfile.TemporaryDirectory(prefix="quickcode-workspace-") as scratch:
        root = Path(scratch)
        # Set before importing QuickCode: config path defaults are evaluated at import.
        os.environ["USERPROFILE"] = scratch
        os.environ["HOME"] = scratch
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

        import json

        import uvicorn

        from quickcode.config import Config
        from quickcode.core.events import TextDelta, ToolCallEnd, TurnDone
        from quickcode.providers.base import ModelInfo
        from quickcode.server.app import create_app
        from quickcode.server.projects import ProjectHub, ProjectRegistry
        from quickcode.update import AUTO_CHECK_KEY, PLUGIN_ID

        class PreviewProvider:
            calls = 0

            async def list_models(self):
                return [ModelInfo(id="preview/agent", name="Preview agent", context_length=100_000)]

            async def stream_chat(self, req):
                last = req.messages[-1] if req.messages else None
                if jobs and last is not None and last.role == "user":
                    PreviewProvider.calls += 1
                    python = Path(sys.executable).as_posix()
                    yield ToolCallEnd(
                        id=f"job{PreviewProvider.calls}", name="bash",
                        arguments=json.dumps({
                            "command": f'"{python}" -u ticker.py',
                            "description": "Tick until stopped",
                            "run_in_background": True,
                        }),
                    )
                    yield TurnDone("tool_calls")
                    return
                for text in ["I am reviewing this project. ", "The workspace and agent panes ",
                             "keep separate conversations, drafts, and settings."]:
                    await asyncio.sleep(.5)
                    yield TextDelta(text)
                yield TurnDone("stop")

        async def serve():
            cfg = Config(last_model="preview/agent")
            cfg.save()
            # The update check's off switch is a plugin setting, not a config key.
            settings = root / ".quickcode" / "settings.json"
            settings.write_text(json.dumps({
                "plugins": {PLUGIN_ID: {"settings": {AUTO_CHECK_KEY: False}}},
            }), encoding="utf-8")
            hub = ProjectHub(config=cfg, provider=PreviewProvider(), registry=ProjectRegistry.ephemeral())
            for name in ["Website redesign", "Client portal"]:
                project = root / name
                project.mkdir()
                (project / "README.md").write_text(f"# {name}\nUI review project.\n", encoding="utf-8")
                if jobs:
                    (project / "ticker.py").write_text(TICKER, encoding="utf-8")
                await hub.open(project)
            port = int(positional[0]) if positional else 8769
            print(f"http://127.0.0.1:{port}/#token=workspace-preview", flush=True)
            app = create_app(hub, host="127.0.0.1", port=port, token="workspace-preview")
            await uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")).serve()

        asyncio.run(serve())


if __name__ == "__main__":
    main()
