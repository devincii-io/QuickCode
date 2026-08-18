"""Permission profiles: a named permission posture, switchable in one action.

A profile is not a preset. ``kernel/preset.py`` opens by saying what a preset
is -- which agent you are talking to, which tools it has, which subagents it
may spawn -- and it carries a ``default_mode`` only because a composition has
to start somewhere. A profile answers the other question: *how much may this
session do on its own, right now*. The same preset is worth running under
"Read only" while you review a pull request and under "Build and test" ten
minutes later, and nothing about the agent's tools or prompt changes in
between. Keeping them apart is why a preset may name a profile but a profile
knows nothing about tools.

``core/permissions.py`` already decides everything -- the modes, the three rule
lists, deny-beats-allow, bash decomposition -- and this module reimplements
none of it. It produces a ``Mode`` and a ``Rules`` and hands them over. What
was missing was never the granularity: ``allow: ["bash(git *)"]`` has always
worked. It was that such a combination could not be named, saved, listed or
switched to without hand-editing a settings file.

Two consequences worth reading before authoring one:

**A profile restricts by denying, not by omitting.** Its rules merge with the
project's own instead of replacing them, so a rule the user accreted through
"always allow" survives switching profiles. That leaves ``deny`` and ``ask`` as
the only ways a profile narrows anything -- which is also the engine's
strongest verb, and the reason a profile is safe to load from a repository
nobody has vouched for.

**``mode`` is where the session starts, not a ceiling**, exactly as a preset's
``default_mode`` is: Shift+Tab still works. So a profile that means to hold has
to say so in rules. "Read only" denies ``write`` and ``edit`` outright rather
than trusting plan mode to still be the mode ten minutes from now.

Built-ins are defined here; user profiles live in ``~/.quickcode/settings.json``
and project ones in ``<project>/.quickcode/settings.json`` under ``profiles``,
with ``active_profile`` naming the selected one -- the same layering, and the
same trust gate, as ``presets``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quickcode.core.permissions import Mode, Rules
from quickcode.kernel.problems import Layer, Problem, Provenance
from quickcode.kernel.state import _read, project_settings_path, user_settings_path
from quickcode.security import trust
from quickcode.security.trust import project_may_state

log = logging.getLogger("quickcode.core.profiles")

PROFILES_KEY = "profiles"
ACTIVE_KEY = "active_profile"

# Problem codes, in the vocabulary ``kernel/problems.py`` defines. Declared
# here rather than there because a code belongs with the thing that raises it.
PROFILE_INVALID = "profile_invalid"
PROFILE_REFUSED = "profile_refused"

# The rule grammar ``permissions._rule_matches`` actually implements: a bare
# tool name, or ``tool(pattern)``. Restated here rather than imported because
# the engine parses to *match* and this parses to *reject* -- it has no opinion
# about what the pattern means, only about whether the engine could ever look
# at it. Anything else the engine silently reads as a bare tool name that no
# tool is called, so it matches nothing, forever, without saying so.
_RULE_SHAPE = re.compile(r"\w+(\(.*\))?")


def _clean_rules(raw: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split one authored rule list into the usable rules and the junk.

    Dropping the bad line rather than the whole list is the choice a rule list
    invites: the entries are independent statements, and losing nine good ones
    to a missing bracket in the tenth would be a worse failure than the typo.
    """
    if raw is None:
        return (), ()
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        # A single string where a list belongs is the common hand-edit; it is
        # still not a rule list, and guessing which one it meant is not our job.
        return (), (str(raw),)
    good: list[str] = []
    bad: list[str] = []
    for item in raw:
        text = item.strip() if isinstance(item, str) else ""
        (good if text and _RULE_SHAPE.fullmatch(text) else bad).append(
            text or str(item)
        )
    return tuple(good), tuple(bad)


@dataclass(frozen=True)
class PermissionProfile:
    """A named bundle of a starting mode and the three rule lists.

    Everything below ``builtin`` is loader output rather than authored content:
    it is what the UI needs to render this profile as the thing it *is* after
    the trust gate and the validator have had it, instead of the thing the file
    asked for.
    """

    id: str
    title: str
    description: str = ""
    # Stored as the string the file uses, coerced on demand -- same as
    # ``Preset.default_mode``, so a file written by a newer build naming a mode
    # this one has never heard of degrades instead of failing to parse.
    mode: str = Mode.ask.value
    allow: tuple[str, ...] = ()
    ask: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    builtin: bool = False

    # Which file this came from. Distinct from ``builtin``: a user profile that
    # reuses a built-in id is not built-in, and the picker has to be able to say
    # so. Also what tells the UI whether the Edit button should exist.
    layer: Layer = "default"
    # Fields the trust gate dropped, by the name the file writes them under.
    refused: tuple[str, ...] = ()
    # Authored values the validator could not use, verbatim, so the message can
    # quote the user's own text back at them.
    invalid: tuple[str, ...] = ()

    @property
    def widens(self) -> bool:
        """Whether adopting this profile lets the session do more on its own.

        The one question the trust gate asks. Both halves of the answer are
        the gate's own: an allow rule is the widening list by definition, and
        ``project_may_state`` owns which modes count as asking for more.
        """
        return bool(self.allow) or not project_may_state("default_mode", self.mode)

    def mode_enum(self) -> Mode:
        try:
            return Mode(self.mode)
        except ValueError:
            return Mode.ask

    def merged(self, base: Rules | None = None) -> Rules:
        """This profile's rules over the session's own.

        Merged rather than substituted so that "always allow" decisions the
        user made in this project are not quietly revoked by picking a profile.
        The module docstring says what that costs: a profile narrows by saying
        ``deny``, never by leaving something out.
        """
        base = base or Rules()
        return Rules(
            allow=list(base.allow) + list(self.allow),
            ask=list(base.ask) + list(self.ask),
            deny=list(base.deny) + list(self.deny),
        )

    def to_dict(self) -> dict[str, Any]:
        """The on-disk body -- authored fields only.

        ``builtin``, ``layer``, ``refused`` and ``invalid`` are answers about
        *this* load on *this* machine. Writing them would let a file assert
        them back, which for ``builtin`` in particular is a claim no file gets
        to make about itself.
        """
        return {
            "title": self.title,
            "description": self.description,
            "mode": self.mode,
            "allow": list(self.allow),
            "ask": list(self.ask),
            "deny": list(self.deny),
        }

    def to_json(self) -> dict[str, Any]:
        """The full payload the frontend renders, refusals included."""
        return {
            "id": self.id,
            **self.to_dict(),
            "builtin": self.builtin,
            "layer": self.layer,
            "refused": list(self.refused),
            "invalid": list(self.invalid),
            "widens": self.widens,
        }

    @classmethod
    def from_dict(cls, profile_id: str, raw: dict[str, Any], *,
                  gated: bool = False, layer: Layer = "user") -> PermissionProfile:
        """Parse one authored profile. Never raises.

        The validator repairs a field it cannot understand and rejects an entry
        it cannot identify, which is the same line drawn twice: a profile with
        a mistyped mode is still a profile and still worth having, while a
        profile with no id is a thing nobody can select, name in
        ``active_profile``, or be told about afterwards. The repair is always
        toward ``ask``, the mode that asks about everything -- not because it
        is what the author meant, which is unknowable, but because it is the
        one value that cannot act on a guess.

        ``gated`` is the untrusted-project path: the widening half is dropped
        and recorded, exactly as ``Rules.load`` and ``preset._presets_from``
        drop theirs.
        """
        raw = raw if isinstance(raw, dict) else {}
        invalid: list[str] = []

        allow, bad = _clean_rules(raw.get("allow"))
        invalid += [f"allow: {r}" for r in bad]
        ask, bad = _clean_rules(raw.get("ask"))
        invalid += [f"ask: {r}" for r in bad]
        deny, bad = _clean_rules(raw.get("deny"))
        invalid += [f"deny: {r}" for r in bad]

        mode_raw = raw.get("mode")
        mode = mode_raw.strip() if isinstance(mode_raw, str) else ""
        if mode and mode not in {m.value for m in Mode}:
            invalid.append(f"mode: {mode}")
            mode = ""
        mode = mode or Mode.ask.value

        refused: list[str] = []
        if gated:
            if allow:
                refused.append("allow")
                allow = ()
            if not project_may_state("default_mode", mode):
                refused.append("mode")
                mode = Mode.ask.value

        title = raw.get("title")
        return cls(
            id=profile_id,
            title=str(title).strip() if isinstance(title, str) and title.strip()
            else profile_id,
            description=str(raw.get("description") or ""),
            mode=mode,
            allow=allow,
            ask=ask,
            deny=deny,
            builtin=False,
            layer=layer,
            refused=tuple(refused),
            invalid=tuple(invalid),
        )

    def problems(self, provenance: Provenance | None = None) -> list[Problem]:
        """What this profile lost on the way in, as reportable problems.

        The refusal has to be *findable*. A profile that quietly means less
        than the file in front of you says is the defect
        ``state.untrusted_project_problems`` exists to fix, one layer down, and
        a profile is worse than a plugin setting for it because the user picked
        this one by name and expects the name to hold.
        """
        out: list[Problem] = []
        if self.refused:
            listed = " and ".join(self.refused)
            out.append(Problem(
                code=PROFILE_REFUSED,
                severity="warning",
                message=(
                    f"profile {self.id!r} is defined by a project that is not "
                    f"trusted, so its {listed} "
                    f"{'were' if len(self.refused) != 1 else 'was'} ignored; it "
                    "can narrow what the agent may do but not widen it"
                ),
                fix=("Read the profile, then trust this project to let it "
                     "apply. An allow rule or a permissive base mode widens "
                     "what the agent may do without asking, which is the same "
                     "decision trusting an MCP server is."),
                subject=self.id, field=self.refused[0], provenance=provenance,
            ))
        if self.invalid:
            count = len(self.invalid)
            out.append(Problem(
                code=PROFILE_INVALID,
                severity="warning",
                message=(
                    f"profile {self.id!r} has {count} "
                    f"{'entries' if count != 1 else 'entry'} the permission "
                    f"engine can never match ({', '.join(self.invalid)}); "
                    f"{'they were' if count != 1 else 'it was'} dropped and "
                    "the rest of the profile applies"
                ),
                fix=("A rule is a tool name, or a tool name with a pattern in "
                     "brackets: write, bash(git *), read(src/**)."),
                subject=self.id, field="rules", provenance=provenance,
            ))
        return out


def builtin_profiles() -> dict[str, PermissionProfile]:
    """The postures worth shipping.

    Each earns its place by being a combination somebody actually works in and
    that no single mode expresses. Deliberately not here: a profile whose whole
    content is ``mode: yolo`` or ``mode: auto-edit``. Those are modes, they
    already have a keystroke, and wrapping one in a name would make the picker
    a second, worse mode selector.
    """
    return {
        "readonly": PermissionProfile(
            id="readonly",
            title="Read only",
            description=(
                "Read, search and list. Writing and network access are denied "
                "outright; the shell stays read-only for as long as the "
                "session stays in plan mode. For a pull request, an audit, or "
                "a repository you have just cloned and not decided about yet."
            ),
            builtin=True,
            mode=Mode.plan.value,
            # Plan mode already denies all four. The rules are what makes them
            # stay denied through the Shift+Tab that plan mode is one keystroke
            # away from -- the difference between a mode and a stance.
            #
            # The shell is the one thing left to the mode, and the description
            # says so rather than claiming otherwise: `deny: bash` is checked
            # before the read-only auto-allow, so it would take `ls` and `rg`
            # with it, and there is no way to spell "deny bash except the
            # harmless ones" that the engine does not already spell better.
            deny=("write", "edit", "web_fetch", "web_search"),
        ),
        "git": PermissionProfile(
            id="git",
            title="Git only",
            description=(
                "Every git command runs without asking; everything else still "
                "prompts. For staging, branching and history work, where the "
                "prompt-per-command tax is highest and the risk is lowest."
            ),
            builtin=True,
            allow=("bash(git **)",),
            # The three git commands that destroy work rather than record it.
            # ``ask`` is checked before ``allow`` in the shell path, so these
            # carve back out of the blanket grant above rather than fighting it.
            ask=("bash(git push**)", "bash(git reset --hard**)",
                 "bash(git clean**)"),
        ),
        "survey": PermissionProfile(
            id="survey",
            title="Survey",
            description=(
                "Names, not contents: list and match paths anywhere in the "
                "tree, but do not open a file. For mapping an unfamiliar "
                "codebase, or one whose contents should not enter a transcript."
            ),
            builtin=True,
            # `glob` needs no allow rule -- a non-mutating tool is allowed in
            # every mode already, and a protected path prompts ahead of any
            # rule either way. The whole profile is the deny list, which is why
            # it is also the one built-in an untrusted project can state in full.
            #
            # The bash entries are the ones that matter: `cat`, `head`, `tail`,
            # `rg` and `grep` are read-only builtins that auto-allow, so
            # denying `read` and `grep` without them would close the front door
            # and leave the shell wide open.
            deny=("read(**)", "grep(**)", "bash(cat **)", "bash(head **)",
                  "bash(tail **)", "bash(less **)", "bash(more **)",
                  "bash(rg **)", "bash(grep **)"),
        ),
        "build": PermissionProfile(
            id="build",
            title="Build and test",
            description=(
                "The test suite, the linter and the build run unprompted; "
                "edits and everything else still ask. For the edit-run-read "
                "loop, where the same handful of commands is approved dozens "
                "of times an hour."
            ),
            builtin=True,
            # Named runners only. `make`, `npm run <anything>` and `uv run
            # <anything>` were left out on purpose: each takes an arbitrary
            # program out of a file in the repository, so allowing them is
            # allowing whatever that file says today, which is not a decision
            # this list can make on the user's behalf.
            allow=(
                "bash(uv run pytest**)", "bash(pytest**)",
                "bash(uv run ruff**)", "bash(ruff**)",
                "bash(npm test**)", "bash(npm run build**)",
                "bash(npm run lint**)", "bash(npm run test**)",
                "bash(cargo test**)", "bash(cargo build**)",
                "bash(cargo clippy**)",
                "bash(go test**)", "bash(go build**)",
            ),
        ),
    }


def _parse_layer(raw: dict[str, Any], *, layer: Layer, gated: bool, path: Path,
                 existing: dict[str, PermissionProfile],
                 ) -> tuple[dict[str, PermissionProfile], list[Problem]]:
    """One settings file's profiles, plus what it lost getting here."""
    out: dict[str, PermissionProfile] = {}
    problems: list[Problem] = []
    section = raw.get(PROFILES_KEY)
    if not isinstance(section, dict):
        return out, problems
    provenance = Provenance(layer=layer, source=path.name, path=str(path))

    for raw_id, body in section.items():
        profile_id = str(raw_id).strip()
        if not profile_id or not isinstance(body, dict):
            problems.append(Problem(
                code=PROFILE_INVALID, severity="warning",
                message=(
                    "a profile entry has no name and was skipped"
                    if not profile_id else
                    f"profile {profile_id!r} is not an object and was skipped"
                ),
                fix='Each profile is an object keyed by its id: '
                    '"profiles": {"my-profile": {"title": "...", "mode": "ask"}}.',
                subject=profile_id or "(unnamed)", field="id",
                provenance=provenance,
            ))
            continue
        if gated and profile_id in existing:
            # Redefining a name is widening even when every field in the new
            # body looks narrow: the user picks "Read only" by its title and
            # relies on the title. So an untrusted project may add profiles but
            # never replace one that already exists.
            problems.append(Problem(
                code=PROFILE_REFUSED, severity="warning",
                message=(
                    f"this project redefines the existing profile "
                    f"{profile_id!r} and is not trusted, so its definition is "
                    "ignored and the original applies"
                ),
                fix=("Trust this project to let it redefine the profile, or "
                     "rename the project's own copy."),
                subject=profile_id, field="id", provenance=provenance,
            ))
            continue
        profile = PermissionProfile.from_dict(
            profile_id, body, gated=gated, layer=layer,
        )
        out[profile_id] = profile
        problems += profile.problems(provenance)
    return out, problems


def _load(cwd: Path | None, *, trusted: bool | None = None,
          ) -> tuple[dict[str, PermissionProfile], list[Problem]]:
    """Built-ins, then user, then project -- each shadowing, one computation.

    Profiles and problems come out of the same pass because they are the same
    fact seen twice: what got loaded, and what did not.
    """
    from quickcode.security import trust

    profiles = builtin_profiles()
    problems: list[Problem] = []

    path = user_settings_path()
    found, probs = _parse_layer(_read(path), layer="user", gated=False,
                                path=path, existing=profiles)
    profiles.update(found)
    problems += probs

    if cwd is not None:
        path = project_settings_path(cwd)
        found, probs = _parse_layer(
            _read(path), layer="project",
            gated=not trust.resolve_trust(cwd, trusted),
            path=path, existing=profiles,
        )
        profiles.update(found)
        problems += probs
    return profiles, problems


def load_profiles(cwd: Path | None, *, trusted: bool | None = None,
                  ) -> dict[str, PermissionProfile]:
    """Every profile this machine and this project offer, by id."""
    return _load(cwd, trusted=trusted)[0]


def profile_problems(cwd: Path | None, *, trusted: bool | None = None,
                     ) -> list[Problem]:
    """Everything a profile asked for and did not get, ready to render."""
    return _load(cwd, trusted=trusted)[1]


def active_profile_id(cwd: Path | None, *, trusted: bool | None = None) -> str:
    """The selected profile's id, project file first, then the user's.

    The same asymmetry ``preset.active_preset_id`` documents: the most specific
    file *names* the active one, and the most specific file *wins* when
    defining one.
    """
    from quickcode.security import trust

    layers: list[tuple[bool, dict[str, Any]]] = []
    if cwd is not None:
        layers.append((not trust.resolve_trust(cwd, trusted),
                       _read(project_settings_path(cwd))))
    layers.append((False, _read(user_settings_path())))

    for gated, raw in layers:
        value = raw.get(ACTIVE_KEY)
        if not isinstance(value, str) or not value:
            continue
        if gated and _widening_selection(cwd, value, trusted=trusted):
            # Selecting is its own grant. A project that cannot write an allow
            # rule can otherwise get one by pointing ``active_profile`` at a
            # permissive profile the *user* wrote, which is the same widening
            # with an extra step.
            log.warning(
                "project %s is not trusted; ignoring active_profile %r, which "
                "would widen what the agent may do without asking", cwd, value,
            )
            continue
        return value
    return ""


def _widening_selection(cwd: Path | None, profile_id: str, *,
                        trusted: bool | None = None) -> bool:
    profile = load_profiles(cwd, trusted=trusted).get(profile_id)
    return profile is not None and profile.widens


def resolve(cwd: Path | None, profile_id: str = "", *,
            trusted: bool | None = None) -> PermissionProfile | None:
    """The profile to run, or ``None`` when none is selected.

    ``None`` is a real answer and the default one: a session with no profile
    behaves exactly as it did before profiles existed. A profile that was named
    but has since been deleted resolves to ``None`` too, with a line in the
    log -- losing a posture is bad, refusing to open the session is worse, and
    the fallback is the safe direction either way.
    """
    profiles = load_profiles(cwd, trusted=trusted)
    wanted = profile_id or active_profile_id(cwd, trusted=trusted)
    if not wanted:
        return None
    profile = profiles.get(wanted)
    if profile is None:
        log.warning("permission profile %r not found; running without one", wanted)
    return profile


def effective(cwd: Path | None, base: Rules, *, profile_id: str = "",
              fallback: Mode = Mode.ask, trusted: bool | None = None,
              ) -> tuple[Mode, Rules, PermissionProfile | None]:
    """The starting mode and the rules a session runs, profile applied.

    ``fallback`` is the mode to keep when no profile is active: the caller has
    already resolved one from its preset and settings, and a session without a
    profile must come out of here unchanged.
    """
    profile = resolve(cwd, profile_id, trusted=trusted)
    if profile is None:
        return fallback, base, None
    return profile.mode_enum(), profile.merged(base), profile


# ---- persistence ----------------------------------------------------------

def _settings_path(cwd: Path | None) -> Path:
    """Project file when there is a project, the user's own otherwise."""
    return project_settings_path(cwd) if cwd is not None else user_settings_path()


def _write(path: Path, raw: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")


def save_profile(profile: PermissionProfile, *, cwd: Path | None = None) -> None:
    """Write a profile into the project settings file, or the user's own.

    Everything else in the file is read, updated in place and written back, the
    way ``state.save_entry`` does it: this owns one key and must not clobber
    permissions, presets or MCP servers on its way past them.
    """
    path = _settings_path(cwd)
    raw = _read(path)
    section = raw.get(PROFILES_KEY)
    if not isinstance(section, dict):
        section = {}
    # A widening profile is part of the trust hash, so writing one moves the
    # hash and would revoke the project's grant. That is right when a
    # repository ships a profile and wrong when the user just wrote one
    # here -- re-asking someone to trust a project because of an edit they
    # made in this app teaches them to click through the prompt.
    #
    # Re-affirmed only if the project was *already* trusted before the write.
    # Granting on save would turn "save a profile" into a way to trust a
    # project without ever being asked, which is the bypass this gate exists
    # to close.
    was_trusted = bool(cwd) and trust.is_trusted(cwd)
    section[profile.id] = profile.to_dict()
    raw[PROFILES_KEY] = section
    _write(path, raw)
    if was_trusted and cwd:
        trust.default_store().grant(cwd)


def delete_profile(profile_id: str, *, cwd: Path | None = None) -> bool:
    """Remove one profile from a settings file. True if it was there.

    A built-in cannot be deleted, only shadowed; this removes the shadow, at
    which point the built-in is back.
    """
    path = _settings_path(cwd)
    raw = _read(path)
    section = raw.get(PROFILES_KEY)
    if not isinstance(section, dict) or profile_id not in section:
        return False
    section.pop(profile_id)
    raw[PROFILES_KEY] = section
    _write(path, raw)
    return True


def set_active(profile_id: str, *, cwd: Path | None = None) -> None:
    """Select a profile, or pass ``""`` to run without one."""
    path = _settings_path(cwd)
    raw = _read(path)
    raw[ACTIVE_KEY] = profile_id
    _write(path, raw)


# What the trust gate needs in order to hash and report a project's profiles.
# It lives here so the shape of a profile is described in exactly one file, and
# is a pure function over an already-parsed settings dict so that
# ``security.trust`` -- which sits below the kernel and cannot import it -- can
# call it without taking on this module's imports.
def policy_keys_from_settings(data: dict[str, Any]) -> dict[str, Any]:
    """The widening half of the profiles one settings file declares.

    Keyed by the dotted path it is written at, matching
    ``trust.project_policy_config``: a grant is bound to the values, so adding
    an allow rule to a profile in a project that was already trusted has to
    re-prompt rather than inherit the old approval.
    """
    out: dict[str, Any] = {}
    section = data.get(PROFILES_KEY) if isinstance(data, dict) else None
    if not isinstance(section, dict):
        return out
    for profile_id, body in section.items():
        if not isinstance(body, dict):
            continue
        rules = body.get("allow")
        if isinstance(rules, list) and rules:
            out[f"{PROFILES_KEY}.{profile_id}.allow"] = [str(r) for r in rules]
        mode = body.get("mode")
        if mode and not project_may_state("default_mode", mode):
            out[f"{PROFILES_KEY}.{profile_id}.mode"] = mode
    return out
