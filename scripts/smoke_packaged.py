"""Verify the frozen app with disposable configuration and project data."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import time
import tomllib
import urllib.request
from pathlib import Path

from quickcode.secrets import API_KEY_ENV
from quickcode.update import AUTO_CHECK_KEY, PLUGIN_ID


def main() -> None:
    repo = Path(__file__).resolve().parents[1]
    expected = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    exe = repo / "dist" / "QuickCode" / "quickcode.exe"
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with tempfile.TemporaryDirectory(prefix="quickcode-packaged-") as scratch:
        root = Path(scratch)
        env = {**os.environ, "USERPROFILE": scratch, "HOME": scratch}
        # No real key reaches the frozen app, and the endpoint it would go to
        # is offline anyway. The key's variable name is fixed (secrets.py).
        env.pop(API_KEY_ENV, None)
        config = root / ".quickcode"
        config.mkdir()
        (config / "config.json").write_text(json.dumps({
            "profiles": {"default": {"base_url": "http://127.0.0.1:1/v1"}},
        }), encoding="utf-8")
        # The update check's off switch is a plugin setting, not a config key;
        # /api/update below then proves the frozen updater loads and asks nothing.
        (config / "settings.json").write_text(json.dumps({
            "plugins": {PLUGIN_ID: {"settings": {AUTO_CHECK_KEY: False}}},
        }), encoding="utf-8")
        version = subprocess.run([str(exe), "--version"], env=env, capture_output=True,
                                 text=True, check=True, timeout=30, creationflags=flags).stdout.strip()
        assert expected in version, (version, expected)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with (root / "server.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen([str(exe), "--cwd", scratch, "--port", str(port), "--no-browser"],
                                       env=env, stdout=log, stderr=log, creationflags=flags)
            try:
                origin = f"http://127.0.0.1:{port}"
                deadline = time.monotonic() + 40
                while True:
                    try:
                        with urllib.request.urlopen(origin + "/api/health", timeout=1) as response:
                            health = json.load(response)
                        break
                    except OSError:
                        if process.poll() is not None or time.monotonic() > deadline:
                            raise RuntimeError("Frozen server did not start") from None
                        time.sleep(.15)
                assert health["version"] == expected, health
                token = (config / "runtime.token").read_text(encoding="utf-8").strip()
                for path in ["/api/bootstrap", "/api/projects", "/api/credits", "/api/update"]:
                    request = urllib.request.Request(origin + path, headers={"x-quickcode-token": token})
                    with urllib.request.urlopen(request, timeout=10) as response:
                        assert response.status == 200, path
                        body = json.load(response)
                    if path == "/api/update":
                        assert body["state"] == "disabled", body
                for path in ["/", "/js/entry.js", "/js/workspaces.js", "/js/split_tree.js",
                             "/js/appearance.js", "/css/workspace.css", "/js/help/workspaces.js"]:
                    with urllib.request.urlopen(origin + path, timeout=5) as response:
                        assert response.status == 200, path
                        assert response.headers["Cache-Control"] == "no-cache", path
                        assert len(response.read()) > 50, path
                print(f"Frozen QuickCode {expected}: version, health, authenticated routes, updater, workspace assets and cache headers passed.")
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    main()
