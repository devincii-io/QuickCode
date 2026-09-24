"""Permission profiles over HTTP: list, save, delete, and select one.

Project-scoped like presets, and for the same reason: a profile lives in a
settings file that belongs to a directory, and the trust gate that decides how
much of one a project may state is a fact about that directory. The four
built-ins ride along in every answer so the picker never has to know they exist.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request

from quickcode.server.http import read_json, scoped, valid_profile_id
from quickcode.server.manager import ConversationManager
from quickcode.server.projects import ProjectHub


def profiles(manager: ConversationManager) -> dict:
    from quickcode.core import profiles as profiles_module
    from quickcode.security import trust

    cwd = manager.cwd
    found = profiles_module.load_profiles(cwd)
    return {
        "profiles": [p.to_json() for p in found.values()],
        "active": profiles_module.active_profile_id(cwd),
        # What a profile asked for and did not get. The same array the
        # kernel's problems renderer already draws, so a refused profile
        # reads the same wherever it is shown.
        "problems": [p.to_json() for p in profiles_module.profile_problems(cwd)],
        # Whether this project has been trusted, because selecting a profile
        # that widens is gated on it (see ``set_active_profile``). Sent so
        # the picker can grey the ones that would be refused instead of
        # discovering it on the click -- each profile already carries the
        # ``widens`` half of that answer.
        "trusted": trust.resolve_trust(cwd),
    }


def _profile_from_body(body: Any):
    """One authored profile out of a request body, or a 400 saying why.

    The parsing is ``PermissionProfile.from_dict``'s, not a second copy of
    it written in route vocabulary: the loader is what decides whether a
    rule is a rule, and an API that disagreed with it would accept things
    that later vanish or refuse things that work when hand-written. The
    difference is only what happens next -- the loader repairs and reports,
    this refuses, because a POST has an author on the other end of it who
    can fix the typo now.
    """
    from quickcode.core.profiles import PermissionProfile

    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")
    raw_id = body.get("id")
    profile_id = raw_id.strip() if isinstance(raw_id, str) else ""
    if not valid_profile_id(profile_id):
        raise HTTPException(400, (
            f"{raw_id!r} is not a usable profile id: it must start with a "
            "letter or digit and may then contain letters, digits, dots, "
            "dashes and underscores"
        ))
    scope = "project" if body.get("scope") == "project" else "user"
    profile = PermissionProfile.from_dict(
        profile_id, body, layer=scope,  # type: ignore[arg-type]
    )
    if profile.invalid:
        count = len(profile.invalid)
        raise HTTPException(400, (
            f"this profile has {count} "
            f"{'entries' if count != 1 else 'entry'} the permission engine "
            f"can never match ({', '.join(profile.invalid)}). A rule is a "
            "tool name, or a tool name with a pattern in brackets: write, "
            "bash(git *), read(src/**); a mode is one of plan, ask, "
            "auto-edit, dontask, yolo"
        ))
    return profile, scope


async def save_profile(manager: ConversationManager, request: Request) -> dict:
    from quickcode.core import profiles as profiles_module

    body = await read_json(request)
    profile, scope = _profile_from_body(body)
    cwd = manager.cwd if scope == "project" else None
    if profile.id in profiles_module.builtin_profiles() and not body.get("shadow"):
        # 409 rather than 400 or a silent write, the same shape
        # ``kernel_api.plugin_update`` uses for a confirmable refusal: the
        # request is valid, it just does something the user has to have meant.
        # A built-in cannot be edited in place -- saving over its id writes a
        # *shadow* that hides it everywhere, including in the picker, where
        # the title stays the same and the meaning does not. Deleting the
        # shadow brings the built-in back, so this is reversible; it is
        # still not something to do by accident, and the UI's Duplicate
        # button offers a fresh id instead precisely so nobody has to.
        raise HTTPException(409, (
            f"{profile.id!r} is a built-in profile. Saving over it writes a "
            f"copy at {scope} scope that hides the built-in under the same "
            "name. Send 'shadow': true to do that deliberately, or save "
            "under a different id — Duplicate offers one."
        ))
    from quickcode.kernel.state import project_settings_path, user_settings_path

    profiles_module.save_profile(profile, cwd=cwd)
    return {
        "saved": profile.id,
        "scope": scope,
        "path": str(project_settings_path(cwd) if cwd else user_settings_path()),
        # A profile is read at session open and by the switch route; it is
        # not pushed onto running sessions by an edit, the way editing a
        # composition is not.
        "applies_to": "new sessions, and this one if you switch to it",
        **profiles(manager),
    }


def delete_profile(manager: ConversationManager, profile_id: str,
                   scope: str = "user") -> dict:
    from quickcode.core import profiles as profiles_module

    if scope not in ("user", "project"):
        raise HTTPException(400, f"scope must be 'user' or 'project', not {scope!r}")
    if not valid_profile_id(profile_id):
        raise HTTPException(400, f"{profile_id!r} is not a usable profile id")
    cwd = manager.cwd if scope == "project" else None
    if not profiles_module.delete_profile(profile_id, cwd=cwd):
        # Includes trying to delete a built-in that nothing shadows: there
        # is no file to remove it from, and a built-in is shipped rather
        # than owned.
        raise HTTPException(404, (
            f"there is no profile {profile_id!r} at {scope} scope"
        ))
    return {"deleted": profile_id, "scope": scope, **profiles(manager)}


def _apply_posture(manager: ConversationManager) -> list[dict]:
    """Push the active posture onto every live session in this project.

    Resolved with no explicit id, so ``active_profile_id`` -- and with it
    the gate that refuses a *selection* an untrusted project makes toward a
    widening profile -- is what decides which one applies. Handing the
    requested id straight to the loader would be that same widening with
    one extra step.

    With no profile selected, each session keeps the mode it is in. Clearing
    a posture is not a request to go back to the install default, and moving
    the mode of a running conversation because a file no longer names a
    profile would be a change nobody asked for.
    """
    from quickcode.core import profiles as profiles_module
    from quickcode.core.permissions import Rules

    cwd = manager.cwd
    base = Rules.load(cwd)
    posture = profiles_module.resolve(cwd)
    out = []
    for conv_id, conv in manager.conversations.items():
        applied = conv.apply_posture(
            posture.mode_enum() if posture else conv.agent.mode,
            posture.merged(base) if posture else base,
            posture,
        )
        out.append({"conv_id": conv_id, **applied})
    return out


async def set_active_profile(manager: ConversationManager,
                             request: Request) -> dict:
    from quickcode.core import profiles as profiles_module
    from quickcode.security import trust

    body = await read_json(request)
    raw_id = body.get("id") if isinstance(body, dict) else None
    if not isinstance(raw_id, str):
        raise HTTPException(400, (
            "body must be {'id': <profile id>}; an empty id clears the "
            "selection and the session runs on the project's own rules"
        ))
    profile_id = raw_id.strip()
    cwd = manager.cwd
    found = profiles_module.load_profiles(cwd)
    if profile_id and profile_id not in found:
        raise HTTPException(400, f"no permission profile {profile_id!r}")
    # The selection is written into the project's settings file, and once it
    # is there nothing can tell it apart from the same line committed by the
    # repository -- which is why ``active_profile_id`` gates it. So a
    # selection that would be gated is refused here, at the point where
    # there is still someone to tell: writing it and then reading it back as
    # nothing is the silent failure the whole module is written against.
    #
    # Refused rather than granted, deliberately. Pointing at a profile that
    # lets the agent act without asking is the same decision trusting the
    # project is, and clicking a picker is not how that decision gets made.
    if profile_id and found[profile_id].widens and not trust.resolve_trust(cwd):
        raise HTTPException(409, (
            f"profile {profile_id!r} lets the agent act without asking, and "
            "this project has not been trusted, so selecting it here would "
            "have no effect. Trust the project to use it, or pick a profile "
            "that only narrows -- those apply to any project."
        ))
    profiles_module.set_active(profile_id, cwd=cwd)
    applied = _apply_posture(manager)
    return {"applied_to": applied, **profiles(manager)}


def register_profile_routes(app: FastAPI, hub: ProjectHub) -> None:
    for method, suffix, handler in (
        # Before the ``{profile_id}`` route so the literal ``active`` segment
        # can never be read as a profile id.
        ("POST", "/profiles/active", set_active_profile),
        ("GET", "/profiles", profiles),
        ("POST", "/profiles", save_profile),
        ("DELETE", "/profiles/{profile_id}", delete_profile),
    ):
        scoped(app, hub, method, suffix, handler)
