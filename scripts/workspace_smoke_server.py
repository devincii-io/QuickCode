"""Disposable UI review server. No real provider, credentials, or project files.

Run through uv with --no-sync, then open the printed URL. Ctrl+C stops it.

``--ask`` makes the preview agent act instead of only talking: every turn it
reads README.md, edits it, then runs a shell command, so the permission prompt
(its diff, the rules "Always allow" would save, "Why?") can be reviewed.
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

        import json

        import uvicorn

        from quickcode.config import Config
        from quickcode.core.events import TextDelta, ToolCallEnd, TurnDone
        from quickcode.providers.base import ModelInfo
        from quickcode.server.app import create_app
        from quickcode.server.projects import ProjectHub, ProjectRegistry
        from quickcode.update import AUTO_CHECK_KEY, PLUGIN_ID

        ask = "--ask" in sys.argv[1:]
        # One tool call per round of a turn, in order; then the usual reply.
        acts = [
            ("read", {"file_path": "README.md"}),
            ("edit", {"file_path": "README.md", "old_string": "UI review project.",
                      "new_string": "UI review project, with a permission preview."}),
            ("bash", {"command": "FOO=1 npm test && cat .env"}),
        ]

        class PreviewProvider:
            async def list_models(self):
                return [ModelInfo(id="preview/agent", name="Preview agent", context_length=100_000)]

            async def stream_chat(self, req):
                last_user = max(i for i, m in enumerate(req.messages) if m.role == "user")
                done = sum(m.role == "tool" for m in req.messages[last_user:])
                if ask and done < len(acts):
                    name, args = acts[done]
                    yield ToolCallEnd(f"preview-{last_user}-{done}", name, json.dumps(args))
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
                await hub.open(project)
            ports = [a for a in sys.argv[1:] if not a.startswith("--")]
            port = int(ports[0]) if ports else 8769
            print(f"http://127.0.0.1:{port}/#token=workspace-preview", flush=True)
            app = create_app(hub, host="127.0.0.1", port=port, token="workspace-preview")
            await uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")).serve()

        asyncio.run(serve())


if __name__ == "__main__":
    main()
