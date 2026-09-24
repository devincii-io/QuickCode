"""``POST .../permissions/explain``: would this call be allowed, and why?

A dry run of the permission gate for one tool call, answered by the project's
real ``PermissionEngine`` -- built the way a new session builds it
(``core/permission_posture.for_new_session``), or, given ``conv``, the live gate
of that open conversation with the "always allow" answers it has accrued. The
explanation is ``core/permission_explain.explain``'s; nothing is decided here.

Body::

    {"tool": "bash", "command": "git push origin main"}        # a shell tool
    {"tool": "read", "input": {"file_path": ".env"}}            # any tool's arguments
    {"tool": "edit", "target": "src/app.py"}                    # its declared target

or, for the prompt on screen ("Why?" in the permission dialog), only::

    {"conv": "<conv id>", "review": "<req_id>"}   # the pending call, as it was gated

and, all optional (not with "review"):

    "mode":          ask as if the session were in this mode
    "conv":          ask the live gate of this open conversation
    "rules":         {"allow": [...], "ask": [...], "deny": [...]} added for this
                     question only -- a profile draft, a sandbox
    "project_rules": false leaves out the project's settings rules
    "profile":       false leaves out the active permission profile

Read-only by construction: no tool runs, no hook runs, no rule is written.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from quickcode.core import permission_explain, permission_posture
from quickcode.core.permissions import Mode
from quickcode.server.http import read_json, scoped

# An explain request is a tool name, one call's arguments and a few rules.
BODY_CAP = 64 * 1024


def _str_field(body: dict[str, Any], key: str) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(400, f"{key!r} must be a string")
    return value


def _flag(body: dict[str, Any], key: str) -> bool:
    value = body.get(key, True)
    if not isinstance(value, bool):
        raise HTTPException(400, f"{key!r} must be true or false")
    return value


def _mode(body: dict[str, Any]) -> Mode | None:
    raw = _str_field(body, "mode")
    if not raw:
        return None
    try:
        return Mode(raw)
    except ValueError:
        raise HTTPException(400, (
            f"unknown mode {raw!r}; one of {', '.join(m.value for m in Mode)}"
        )) from None


def _posture(manager: Any, body: dict[str, Any]) -> Any:
    from quickcode.kernel.resolve import session_pool

    tools = list(manager.registry_factory().tools.values())
    project_rules, use_profile = _flag(body, "project_rules"), _flag(body, "profile")
    conv_id = _str_field(body, "conv")
    if not conv_id:
        return permission_posture.for_new_session(
            manager.cwd, config=manager.config, tools=tools,
            default_mode=manager.default_mode, allow_yolo=manager.allow_yolo,
            resolve_model=manager.resolve_role,
            project_rules=project_rules, use_profile=use_profile,
        )
    if not (project_rules and use_profile):
        raise HTTPException(400, (
            "a live conversation's rules are the ones it runs on; add what-if "
            "'rules' instead of leaving layers out"
        ))
    conv = manager.get(conv_id)
    if conv is None:
        raise HTTPException(404, f"no live conversation {conv_id!r}")
    return permission_posture.for_conversation(
        conv, pool=session_pool(manager.cwd, tools), yolo_armed=manager.allow_yolo,
    )


def _explain_review(manager: Any, body: dict[str, Any], review_id: str) -> dict[str, Any]:
    """The pending call itself, asked of the gate that raised it -- the asking
    agent's engine, which for a subagent is not the conversation's."""
    from quickcode.kernel.resolve import session_pool
    from quickcode.security import trust

    extra = sorted(set(body) - {"conv", "review"})
    if extra:
        raise HTTPException(400, (
            f"a pending prompt is explained as it stands; {', '.join(extra)} "
            "cannot be combined with 'review'"
        ))
    conv_id = _str_field(body, "conv")
    conv = manager.get(conv_id) if conv_id else None
    if conv is None:
        raise HTTPException(404, f"no live conversation {conv_id!r}")
    pending = conv.reviews.pending.get(review_id)
    request = getattr(pending, "request", None)
    if request is None or request.gated is None:
        raise HTTPException(404, f"no permission prompt {review_id!r} is waiting")
    tools = list(manager.registry_factory().tools.values())
    posture = permission_posture.for_review(
        conv, request.gated, pool=session_pool(manager.cwd, tools),
        yolo_armed=manager.allow_yolo,
    )
    return permission_explain.explain(posture, request.gated.tool.name, request.gated.args,
                                      trusted=trust.resolve_trust(manager.cwd))


def explain_payload(manager: Any, body: dict[str, Any]) -> dict[str, Any]:
    from quickcode.security import trust

    review_id = _str_field(body, "review")
    if review_id:
        return _explain_review(manager, body, review_id)
    tool_name = _str_field(body, "tool") or ("bash" if body.get("command") is not None else "")
    if not tool_name:
        raise HTTPException(400, "name the tool: {'tool': 'bash', 'command': '...'}")
    try:
        extra = permission_posture.what_if_rules(body.get("rules"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    posture = _posture(manager, body).with_mode(_mode(body)).with_rules(extra)

    tool = posture.tools.get(tool_name)
    if tool is None:
        raise HTTPException(404, (
            f"no tool {tool_name!r} in this project; it has: "
            f"{', '.join(sorted(posture.tools))}"
        ))
    try:
        args = permission_explain.call_arguments(
            tool, input=body.get("input"), command=body.get("command"),
            target=body.get("target"),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return permission_explain.explain(posture, tool_name, args,
                                      trusted=trust.resolve_trust(manager.cwd))


async def explain_permission(manager: Any, request: Request) -> dict[str, Any]:
    body = await read_json(request, maximum=BODY_CAP)
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")
    # Off the loop: building the posture reads settings files and resolves the
    # composition, and a keystroke-driven caller asks often.
    return await asyncio.to_thread(explain_payload, manager, body)


def register_permission_routes(app: FastAPI, hub: Any) -> None:
    scoped(app, hub, "POST", "/permissions/explain", explain_permission)
