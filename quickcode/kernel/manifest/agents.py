"""One plugin per subagent definition, its prose derived from the ceiling and tools."""

from __future__ import annotations

from typing import Any

from quickcode.core.permissions import Mode
from quickcode.kernel.manifest._text import lazy_view
from quickcode.kernel.spec import PluginSpec, Recourse, SettingSpec

# What a ceiling means for a subagent. A subagent has no interactive permission
# callback: anything the engine answers with "ask" is auto-denied, which is why
# an ``ask`` ceiling reads as "never changes files" rather than "asks first".
_CEILING_PROSE: dict[Mode, tuple[str, str]] = {
    Mode.plan: (
        "cannot change anything",
        "A plan ceiling collapses to ask when the agent is spawned, because a "
        "headless child cannot run the plan review dance. In practice it "
        "investigates and reports.",
    ),
    Mode.ask: (
        "never changes files",
        "Anything that would need permission is refused outright: a subagent has "
        "no one to ask, so the engine's \"ask\" becomes a denial the child reads "
        "as a failed tool call.",
    ),
    Mode.auto_edit: (
        "may edit files without asking",
        "File edits go through unprompted. Shell commands are still decomposed "
        "per subcommand, so a command needing a prompt is refused rather than "
        "run.",
    ),
    Mode.dontask: (
        "runs unprompted and refuses whatever would need asking",
        "Nothing prompts and nothing waits: any call the engine would have asked "
        "about is denied instead, including every protected path.",
    ),
    Mode.yolo: (
        "runs without a permission gate",
        "Everything is allowed except the circuit breakers, which stop a "
        "recursive delete or a forced push whatever the mode says.",
    ),
}


def _agent_scope(tools: list[str] | None) -> str:
    """The tool half of an agent's summary, from its declared tool list."""
    if tools is None:
        return "Inherits the spawning agent's tools"
    if not tools:
        return "Has no tools at all"
    if len(tools) <= 3:
        listed = ", ".join(tools[:-1]) + " and " + tools[-1] if len(tools) > 1 else tools[0]
        return f"Limited to {listed}"
    return f"Limited to {len(tools)} named tools"


def _agent_prose(defn: Any) -> dict[str, Any]:
    """Summary, affects, audience and consequence for one agent definition."""
    tools = getattr(defn, "tools", None)
    tools = list(tools) if tools is not None else None
    ceiling = getattr(defn, "mode_cap", Mode.ask)
    if not isinstance(ceiling, Mode):
        try:
            ceiling = Mode(str(ceiling))
        except ValueError:
            ceiling = Mode.ask
    power, ceiling_detail = _CEILING_PROSE[ceiling]

    inherits = tools is None
    scope_note = (
        "Its tool list is whatever the spawner holds at the moment it spawns, "
        "never more, so restricting the spawner restricts this agent too."
        if inherits else
        "Its tool list is intersected with the spawner's, so naming a tool the "
        "spawner does not hold is refused rather than quietly granted."
    )
    return {
        "summary": f"{_agent_scope(tools)}; {power}.",
        "affects": ("tool_list", "permissions", "prompt", "models"),
        "audience": "named_agents",
        "consequence": f"{scope_note} {ceiling_detail}",
        "docs_anchor": "docs/AGENTS.md",
    }


_AGENT_LOCKED = (
    "A built-in definition is what presets and existing sessions resolve "
    "against, so editing it in place would change agents that were spawned "
    "expecting the old one."
)


SHIPPED_AGENTS = ("explore", "general")


def is_shipped_agent(name: str, defn: Any) -> bool:
    """The shipped definition, not a file that took its name.

    ``.quickcode/agents/explore.md`` replaces the built-in at spawn, and the
    loader stamps it with its path and an ``authored`` source. Deciding by name
    presented a repository's file as QuickCode's own, locked and "fixed by
    design".
    """
    return (name in SHIPPED_AGENTS and not getattr(defn, "path", "")
            and getattr(defn, "source", "internal") == "internal")


def agent_specs(defs: dict[str, Any]) -> list[PluginSpec]:
    """One plugin per subagent definition -- built-in and user-authored alike."""
    out: list[PluginSpec] = []
    for name, defn in sorted(defs.items()):
        builtin = is_shipped_agent(name, defn)
        tools = getattr(defn, "tools", None)
        prose = _agent_prose(defn)
        # Provenance is stamped by the loader, never declared by the file. A
        # definition that could name its own source could claim to be internal.
        source = "internal" if builtin else getattr(defn, "source", "config")
        out.append(PluginSpec(
            id=f"agent.{name}",
            kind="agent",
            title=name,
            description=(getattr(defn, "description", "") or "").strip(),
            group="Agents",
            source=source,
            path=getattr(defn, "path", "") or "",
            summary=prose["summary"],
            affects=prose["affects"],
            audience=prose["audience"],
            consequence=prose["consequence"],
            locked_because=_AGENT_LOCKED if builtin else "",
            recourse=Recourse("duplicate", f"Duplicate {name} to get an editable copy",
                              f"agent.{name}") if builtin else None,
            docs_anchor=prose["docs_anchor"],
            settings=(
                SettingSpec(
                    key="model", type="string", default=getattr(defn, "model", "worker"),
                    tier="free", title="Default model",
                    help="A role (worker, orchestrator) or an explicit model slug.",
                    affects=("models",),
                    effect_detail="A role is resolved against the active profile when "
                                  "the agent is spawned; anything else is passed to "
                                  "the provider as written.",
                    example="worker",
                ),
                SettingSpec(
                    key="models", type="list", default=list(getattr(defn, "models", [])),
                    tier="free", title="Allowed models",
                    help="Roles, slugs or globs this agent may run on, one per line. "
                         "Empty means any. Several entries offer a selection.",
                    affects=("models",),
                    effect_detail="The set the default has to be a member of. A caller "
                                  "asking for something outside it is refused rather "
                                  "than silently downgraded.",
                    example="worker",
                ),
                SettingSpec(
                    key="model_selectable", type="bool",
                    default=bool(getattr(defn, "model_selectable", True)),
                    tier="free", title="Caller may choose the model",
                    help="Off pins the agent to its default model; an override is "
                         "refused rather than ignored.",
                    affects=("models",),
                    effect_detail="Governs the agent tool's model argument only. With "
                                  "it off, a spawn naming a model is refused so the "
                                  "caller learns the pin exists.",
                ),
                SettingSpec(
                    key="max_turns", type="int", default=int(getattr(defn, "max_turns", 30)),
                    tier="free", minimum=1, maximum=200, title="Maximum turns",
                    help="The delegation budget one spawned instance of this agent gets.",
                    affects=("loop",),
                    effect_detail="Each spawned instance gets its own budget: the spawn "
                                  "spends one turn and every resume spends another. "
                                  "Past it, a resume is refused and the spawner is told "
                                  "to start a fresh agent.",
                    example="60",
                ),
                SettingSpec(
                    key="mode_cap", type="enum",
                    default=getattr(getattr(defn, "mode_cap", None), "value", "ask"),
                    choices=("plan", "ask", "auto-edit", "dontask", "yolo"),
                    # Tier is a property of a plugin's *source*, not its
                    # content: the tier system protects QuickCode's internals
                    # from you, not your own files from you.
                    tier="confirm" if builtin else "free",
                    title="Permission ceiling",
                    risk="This is the most this agent may ever do, whatever mode the "
                         "session is in. Subagents can never ask you for permission.",
                    affects=("permissions",),
                    effect_detail="The effective mode is the less privileged of this "
                                  "and the spawner's, so raising it here cannot lift "
                                  "an agent above the session it runs in.",
                    example="auto-edit",
                ),
            ),
            metadata={"agent": name, "tools": list(tools) if tools else None,
                      "builtin": builtin,
                      "inherits_tools": tools is None,
                      "models": list(getattr(defn, "models", [])),
                      "model_selectable": bool(getattr(defn, "model_selectable", True))},
            view=lazy_view("markdown", getattr(defn, "prompt_body", "") or "",
                           f"{name} instructions"),
        ))
    return out
