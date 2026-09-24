"""Update checking: is there a newer release, fetch it, run it.

Install-wide, like /api/config: which version is running is not a property of
the directory that happens to be open. Unscoped for the same reason, and never
reached from the agent loop — a check is a REST call the UI makes at boot, so it
cannot interrupt a running turn.

GET is the only route that may talk to github.com, and only when a check is due
(see quickcode/update.py for the interval and the off switch). It never raises
for a dead network: "could not reach it" comes back as a normal 200 with state
"unknown", which is what lets the chrome stay silent about it while the Install
page spells it out.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

from quickcode.server.http import read_json
from quickcode.server.projects import ProjectHub

log = logging.getLogger("quickcode.server")


def register_update_routes(app: FastAPI, hub: ProjectHub) -> None:
    @app.get("/api/update")
    async def update_status(force: bool = False) -> dict:
        from quickcode import update as update_module

        status = await update_module.check(cwd=hub.default.cwd, force=force)
        return status.to_json()

    @app.put("/api/update/settings")
    async def update_settings(request: Request) -> dict:
        from quickcode import update as update_module

        body = await read_json(request)
        if not isinstance(body, dict) or not isinstance(
            body.get(update_module.AUTO_CHECK_KEY), bool
        ):
            raise HTTPException(
                400, f"body must be {{'{update_module.AUTO_CHECK_KEY}': true|false}}"
            )
        try:
            update_module.set_auto_check(body[update_module.AUTO_CHECK_KEY])
        except update_module.UpdateError as exc:
            raise HTTPException(500, str(exc)) from exc
        # Answering with the whole status keeps the page from having to guess
        # what switching it off did to everything else on it.
        status = await update_module.check(cwd=hub.default.cwd)
        return status.to_json()

    @app.post("/api/update/download")
    async def update_download() -> dict:
        """Fetch the release installer and verify it. Never runs anything.

        409 is the checksum refusal, and by the time it is raised the bytes
        are already deleted — see quickcode/update.py.
        """
        from quickcode import update as update_module

        status = await update_module.check(cwd=hub.default.cwd)
        try:
            download = await update_module.download_installer(status)
        except update_module.ChecksumMismatch as exc:
            log.error("refused a downloaded installer: %s", exc)
            raise HTTPException(409, str(exc)) from exc
        except update_module.UpdateError as exc:
            raise HTTPException(400, str(exc)) from exc
        return download.to_json()

    @app.post("/api/update/install")
    async def update_install(request: Request) -> dict:
        """Run a verified installer, on an explicit click and a named digest.

        The body must carry the exact SHA-256 the user was shown beside the
        button, so the confirmation is about specific bytes rather than about
        a filename. The file is hashed again before it is executed.
        """
        from quickcode import update as update_module

        body = await read_json(request)
        if not isinstance(body, dict):
            raise HTTPException(400, "request body must be a JSON object")
        if body.get("confirm") is not True:
            raise HTTPException(400, "body must carry {'confirm': true}")
        path = body.get("path")
        sha256 = body.get("sha256")
        if not isinstance(path, str) or not path.strip():
            raise HTTPException(400, "body must name the downloaded 'path'")
        if not isinstance(sha256, str) or not sha256.strip():
            raise HTTPException(400, "body must carry the 'sha256' that was shown")
        try:
            return update_module.launch_installer(Path(path), expected=sha256)
        except update_module.ChecksumMismatch as exc:
            raise HTTPException(409, str(exc)) from exc
        except update_module.UpdateError as exc:
            raise HTTPException(400, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(500, f"could not start the installer: {exc}") from exc
