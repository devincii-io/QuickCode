"""Disposable UI review server. No real provider, credentials, or project files.

Run through uv with --no-sync, then open the printed URL. Ctrl+C stops it.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="quickcode-workspace-") as scratch:
        root = Path(scratch)
        # Set before importing QuickCode: config path defaults are evaluated at import.
        os.environ["USERPROFILE"] = scratch
        os.environ["HOME"] = scratch
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

        import uvicorn

        from quickcode.config import Config
        from quickcode.core.events import TextDelta, TurnDone
        from quickcode.providers.base import ModelInfo
        from quickcode.server.app import create_app
        from quickcode.server.projects import ProjectHub, ProjectRegistry

        class PreviewProvider:
            async def list_models(self):
                return [ModelInfo(id="preview/agent", name="Preview agent", context_length=100_000)]

            async def stream_chat(self, req):
                for text in ["I am reviewing this project. ", "The workspace and agent panes ",
                             "keep separate conversations, drafts, and settings."]:
                    await asyncio.sleep(.5)
                    yield TextDelta(text)
                yield TurnDone("stop")

        async def serve():
            cfg = Config(last_model="preview/agent")
            cfg.update_check = False
            cfg.save()
            hub = ProjectHub(config=cfg, provider=PreviewProvider(), registry=ProjectRegistry.ephemeral())
            for name in ["Website redesign", "Client portal"]:
                project = root / name
                project.mkdir()
                (project / "README.md").write_text(f"# {name}\nUI review project.\n", encoding="utf-8")
                await hub.open(project)
            port = int(sys.argv[1]) if len(sys.argv) > 1 else 8769
            print(f"http://127.0.0.1:{port}/#token=workspace-preview", flush=True)
            app = create_app(hub, host="127.0.0.1", port=port, token="workspace-preview")
            await uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")).serve()

        asyncio.run(serve())


if __name__ == "__main__":
    main()
