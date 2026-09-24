"""One plugin per live tool, its prose derived from what the tool declares."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from quickcode.core.permissions import DEFAULT_SPEC
from quickcode.kernel.manifest._text import (
    READ_ONLY_LOCKED_BECAUSE,
    READ_ONLY_RECOURSE,
    lazy_view,
    schema_text,
)
from quickcode.kernel.spec import Effect, PluginSpec, Recourse, SettingSpec


def _tool_group(tool: Any) -> str:
    name = getattr(tool, "name", "")
    if name.startswith("mcp__"):
        return "MCP"
    if name in ("bash", "bash_output", "bash_kill"):
        return "Shell"
    if name in ("read", "write", "edit", "glob", "grep"):
        return "Files"
    if name in ("web_fetch", "web_search"):
        return "Web"
    if name.startswith("task"):
        return "Tasks"
    if name in ("agent", "send_message", "agent_status", "agent_result"):
        return "Subagents"
    return "Tools"


# -- tool prose, derived from the tool's own declarations --------------------
#
# A tool declares two things about itself that the runtime reads on every call:
# ``is_read_only``, which decides parallelism, and its ``PermissionSpec``,
# which decides gating. Those two are enough to say something true and specific
# about any tool, including one this file has never heard of, so the character
# below is computed rather than tabulated.

_CHARACTER_PROSE: dict[str, tuple[str, str]] = {
    # character -> (summary, consequence)
    "shell": (
        "Runs commands; each line is split and checked one subcommand at a time.",
        "Compound lines are decomposed and the most restrictive answer wins, so "
        "one dangerous clause gates the whole line. Read-only builtins pass "
        "without a prompt unless the line contains a substitution, which "
        "disqualifies it from every allow path. Withhold it and no agent can "
        "run tests, builds or git.",
    ),
    "file_write": (
        "Changes files on disk, so the path is checked before the call runs.",
        "Withheld entirely in plan mode and prompted for in ask mode. A path "
        "outside the project, or inside .git, .quickcode, .env or .ssh, prompts "
        "before any allow rule is consulted.",
    ),
    "file_read": (
        "Reads from the project; runs without a prompt unless the path is protected.",
        "Allowed by default in every mode and run in parallel with the other "
        "reads in the same round. Protected paths still prompt. Withhold it and "
        "the agent loses this way of seeing the project, not its right to.",
    ),
    "read_only": (
        "Read-only, so it never prompts and runs alongside the other reads.",
        "Declared read-only, so it is allowed in every mode including plan and "
        "runs concurrently with the other read-only calls in the round.",
    ),
    "internal_write": (
        "Writes QuickCode's own bookkeeping rather than your files, so it never asks.",
        "Its target is QuickCode's state rather than the project, so no mode "
        "gates it and only a rule naming the tool outright could. It runs on "
        "its own within a round, after the read-only calls.",
    ),
    "mutating": (
        "Declared as mutating, so it is withheld in plan mode and prompted for.",
        "Nothing about its arguments is a path or a command line, so rules can "
        "only match it by name. A tool that has not declared itself read-only "
        "is prompted for rather than waved through.",
    ),
}

# The facts that live in the wiring rather than in the tool object: which
# agents receive a tool at all. ``registry.build_registry`` never gives a
# subagent ``plan``, and grants the delegation pair by depth rather than by
# allowlist, so no ``PermissionSpec`` could tell you this.
#
# The two web tools are here for the other reason a fact can fail to be
# derivable: a ``PermissionSpec`` has one word for "worth asking about" and it
# is ``mutates``, so the derived prose would say these tools change files. They
# do not. They leave the machine, which is a different thing to be asked about
# and needs saying in its own words.
_TOOL_OVERRIDES: dict[str, dict[str, Any]] = {
    "plan": {
        "audience": "orchestrator",
        "affects": ("tool_list", "ui", "loop"),
        "summary": "Submits a plan for your review; offered only while in plan mode.",
        "consequence": "Answered by the plan review dialog instead of by the tool, so "
                       "it never reaches the permission gate. Subagents never receive "
                       "it -- they have nobody to show a plan to.",
    },
    "agent": {
        "affects": ("tool_list", "loop"),
        "summary": "Spawns a subagent to handle one bounded task and report back.",
        "consequence": "Granted by depth rather than by an allowlist: an agent at the "
                       "depth limit never receives it and a preset cannot hand it out. "
                       "Spawning never prompts; the child's own ceiling gates what the "
                       "child then does. With background=true the call returns a job "
                       "handle instead of a report and the child runs on past the turn "
                       "-- capped by Maximum background jobs at once, cancelled by "
                       "interrupt, collected with agent_result.",
    },
    "agent_status": {
        "affects": ("tool_list", "loop"),
        "summary": "Lists the background subagent jobs and where each one got to.",
        "consequence": "Reads a registry this conversation already owns: no model call, "
                       "no filesystem, nothing to prompt about. Granted by depth "
                       "alongside the spawn tool, because an agent that can start a "
                       "detached job has to be able to ask after it.",
    },
    "agent_result": {
        "affects": ("tool_list", "loop"),
        "summary": "Collects a background subagent's report once the job has finished.",
        "consequence": "Returns exactly what a blocking spawn would have returned -- the "
                       "same sanitized, offloaded report, because a detached run goes "
                       "through the same finishing path. A job still running yields a "
                       "status and no report unless wait_s is passed.",
    },
    "send_message": {
        "affects": ("tool_list", "loop"),
        "summary": "Resumes a finished subagent instead of spawning a fresh one.",
        "consequence": "Granted by depth alongside the spawn tool. The resumed agent "
                       "keeps its full context, which is why this is cheaper than "
                       "spawning the same work again.",
    },
    "bash_output": {
        "affects": ("tool_list", "loop"),
        "summary": "Reads what a background shell job has written since the last read.",
        "consequence": "Reads a buffer this conversation already holds -- no process "
                       "is started, so there is nothing to prompt about. Granted "
                       "wherever bash is, because bash with run_in_background=true "
                       "starts work only this can read. Each job keeps its most "
                       "recent 1 MB of output; anything older than that between two "
                       "reads is dropped, and the read says how much.",
    },
    "bash_kill": {
        "affects": ("tool_list", "loop"),
        "summary": "Stops a background shell job and every process it started.",
        "consequence": "Never prompts: the command was approved when it started, and "
                       "the argument is a job id from this conversation's own table, "
                       "not a pid, so nothing else on the machine can be named. "
                       "Granted wherever bash is. Closing the conversation or "
                       "quitting the app stops every job still running without it.",
    },
    "web_fetch": {
        "affects": ("tool_list", "permissions"),
        "summary": "Reads one public web page as markdown; refuses loopback and "
                   "private addresses.",
        "consequence": "It changes nothing on disk but it does leave the machine, "
                       "which is why it is declared mutating: that is the only word "
                       "the engine has for \"stop and ask\". So it prompts in ask "
                       "mode, is withheld in plan mode, and a subagent capped at ask "
                       "cannot use it at all. Refusals happen before any packet is "
                       "sent -- non-http(s) schemes, loopback, private, link-local and "
                       "reserved addresses, bare and .local hostnames -- and are "
                       "re-checked on every redirect hop, with the connection pinned "
                       "to the address that was checked. No cookies or credentials are "
                       "ever sent. Rules can name a site: web_fetch(https://docs.*/**).",
    },
    "web_search": {
        "affects": ("tool_list", "permissions"),
        "summary": "Runs one query through the configured search provider and "
                   "returns ranked links.",
        "consequence": "Which provider answers is a setting, not an argument: the "
                       "model cannot choose one and nothing falls back to another if "
                       "a key expires. With no provider configured the tool still "
                       "exists and fails with the signup page named, rather than "
                       "disappearing. Like web_fetch it is declared mutating so that "
                       "it prompts, which also withholds it in plan mode. Queries "
                       "spend somebody's monthly quota, and a rule can cap that: "
                       "web_search or web_search(*).",
    },
}

_TOOL_DOCS: dict[str, str] = {
    "read": "docs/TOOLS.md#read-read-only",
    "write": "docs/TOOLS.md#write",
    "edit": "docs/TOOLS.md#edit",
    "glob": "docs/TOOLS.md#glob-read-only",
    "grep": "docs/TOOLS.md#grep-read-only",
    "bash": "docs/TOOLS.md#bash",
    "bash_output": "docs/TOOLS.md#bash_output-read-only",
    "bash_kill": "docs/TOOLS.md#bash_kill",
    "web_fetch": "docs/TOOLS.md#web_fetch",
    "web_search": "docs/TOOLS.md#web_search",
    "agent": "docs/TOOLS.md#agentic-tools-specced-in-docsagentsmd-and-docspermissionsmd",
    "send_message": "docs/TOOLS.md#agentic-tools-specced-in-docsagentsmd-and-docspermissionsmd",
    "agent_status": "docs/TOOLS.md#agentic-tools-specced-in-docsagentsmd-and-docspermissionsmd",
    "agent_result": "docs/TOOLS.md#agentic-tools-specced-in-docsagentsmd-and-docspermissionsmd",
    "plan": "docs/PERMISSIONS.md#plan-mode",
}

_MCP_NOTE = (
    " It comes from an external server process, so it disappears from every "
    "agent's tool list the moment that server is removed."
)

# Why a tool has no editable knobs at all. Different from the reason its
# read-only flag is fixed, and the one a reader meets first on the card.
_TOOL_LOCKED_BECAUSE = (
    "A tool is code. Its schema, its argument validation and its permission "
    "shape all come from the class the runtime instantiates, so a knob here "
    "that changed any of them would be describing a different tool than the "
    "one the model is being handed."
)

_TOOL_RECOURSE = Recourse(
    "settings", "Restrict it with a permission rule, or switch it off entirely",
    "runtime.permissions",
)


def _tool_character(tool: Any) -> str:
    """Which of the six shapes a tool has, from what the tool declares."""
    perm = getattr(tool, "permission", DEFAULT_SPEC)
    read_only = bool(getattr(tool, "is_read_only", False))
    if getattr(perm, "shell", False):
        return "shell"
    if perm.mutates and getattr(perm, "path_target", False):
        return "file_write"
    if perm.mutates:
        return "mutating"
    if getattr(perm, "path_target", False):
        return "file_read"
    if read_only:
        return "read_only"
    return "internal_write"


def _tool_prose(tool: Any) -> dict[str, Any]:
    """Summary, affects, audience and consequence for one live tool."""
    name = getattr(tool, "name", "")
    character = _tool_character(tool)
    summary, consequence = _CHARACTER_PROSE[character]
    perm = getattr(tool, "permission", DEFAULT_SPEC)

    affects: tuple[Effect, ...] = ("tool_list",)
    if perm.mutates or getattr(perm, "shell", False) or getattr(perm, "path_target", False):
        # Anything the permission engine has an opinion about touches the
        # permission surface as well as the tool list.
        affects = ("tool_list", "permissions")

    prose: dict[str, Any] = {
        "summary": summary,
        "affects": affects,
        "audience": "all_agents",
        "consequence": consequence,
        "docs_anchor": _TOOL_DOCS.get(name, "docs/TOOLS.md"),
    }
    if name.startswith("mcp__"):
        prose["consequence"] = consequence + _MCP_NOTE
    prose.update(_TOOL_OVERRIDES.get(name, {}))
    return prose


def tool_specs(tools: Iterable[Any]) -> list[PluginSpec]:
    """One plugin per live tool, with its real schema as the view."""
    out: list[PluginSpec] = []
    for tool in tools:
        name = getattr(tool, "name", "")
        if not name:
            continue
        source = getattr(tool, "source", "internal")
        read_only = bool(getattr(tool, "is_read_only", False))
        prose = _tool_prose(tool)
        payload, signature = schema_text(tool)

        out.append(PluginSpec(
            id=f"tool.{name}",
            kind="tool",
            title=name,
            description=(getattr(tool, "description", "") or "").strip().split("\n")[0],
            group=_tool_group(tool),
            source=source,
            summary=prose["summary"],
            affects=prose["affects"],
            audience=prose["audience"],
            consequence=prose["consequence"],
            locked_because=_TOOL_LOCKED_BECAUSE,
            recourse=_TOOL_RECOURSE,
            docs_anchor=prose["docs_anchor"],
            settings=(
                SettingSpec(
                    key="read_only", type="bool",
                    default=read_only,
                    tier="locked", title="Read-only",
                    help="Read-only tools skip the permission prompt and may run in "
                         "parallel. The tool declares this, not the user.",
                    affects=("loop", "permissions"),
                    effect_detail="Read-only calls in one round are gathered and run "
                                  "together and are allowed by default; everything "
                                  "else runs alone and takes the mode's answer.",
                    locked_because=READ_ONLY_LOCKED_BECAUSE,
                    recourse=READ_ONLY_RECOURSE,
                    # Declared by the tool class, not withheld from you: this
                    # row reports a fact and must not badge the whole card
                    # locked. See PluginSpec.tier.
                    fact=True,
                ),
            ),
            path=getattr(tool, "path", "") or "",
            metadata={"tool_name": name,
                      "read_only": read_only,
                      "character": _tool_character(tool),
                      "signature": signature},
            view=lazy_view("json", payload, f"{name} schema",
                           getattr(tool, "path", "") or ""),
        ))
    return out
