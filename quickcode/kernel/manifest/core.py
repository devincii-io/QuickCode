"""The runtime internals: the loop, permissions, subagents, the session log,
update checking and the composed system prompt.

Each plugin's settings are declared in ``kernel/core_settings.py``, where the
runtime reads them; this module adds what a card says around them.
"""

from __future__ import annotations

from quickcode.kernel.core_settings import CORE_SETTINGS
from quickcode.kernel.manifest._text import lazy_view
from quickcode.kernel.spec import PluginSpec, Recourse

# --------------------------------------------------------------------------
# Locked explanations. These are shown, never edited.
# --------------------------------------------------------------------------

_TOOL_PROTOCOL = """\
QuickCode declares every enabled tool to the model as a JSON Schema derived
from the tool's pydantic Input model, with additionalProperties set to false
so the model cannot invent fields:

    {
      "name": "read",
      "description": "...",
      "parameters": {"type": "object", "properties": {...},
                     "required": [...], "additionalProperties": false}
    }

The model answers with tool calls carrying an id, a name and a JSON argument
string. QuickCode parses the arguments, validates them against the same Input
model, runs the tool, and returns the result in a message tagged with that
same tool_call_id. Read-only tools in one round run concurrently; mutating
tools run one at a time, in the order the model asked for them.

This handshake is what makes a session replayable and auditable, so it is not
configurable. Every part of it is visible here and in the trajectory."""

_SESSION_FORMAT = """\
One append-only JSONL file per conversation at
.quickcode/sessions/<conversation-id>.jsonl, with three interleaved records:

  {"kind": "message", "ts": ..., "message": {...}}   what the model saw
  {"kind": "event",   "ts": ..., "seq": N, "ev": {}} what you saw
  {"kind": "meta",    "ts": ..., ...}                title, model, cwd

The message log is the source of truth on resume; the event log is the source
of truth for the trajectory. Sequence numbers are monotonic and never reused,
which is what lets a reconnecting UI replay a session exactly once.

Append-only is the point: nothing in QuickCode rewrites history, so the format
is fixed."""

_REPORT_SANITIZER = """\
Text coming back from a subagent is untrusted input: it was produced by a
model that may have read files an attacker controls. Before that text re-enters
the parent's context, control tags (system-reminder, task, and friends) are
neutralized so a file cannot smuggle instructions into the orchestrator.

This cannot be switched off."""

_UPDATE_CHECK = """\
The entire request, in full:

    GET https://api.github.com/repos/devincii-io/QuickCode/releases/latest
    Accept: application/vnd.github+json
    X-GitHub-Api-Version: 2022-11-28
    User-Agent: QuickCode

That is all of it. No Authorization header, no cookies, no query string, no
body. GitHub requires a User-Agent, so it gets a fixed string rather than one
carrying a version -- that would have been the only part of the request that
varied per install. Nothing about the machine, the open project, the session,
the model or the user is sent, and there is no second endpoint.

The answer is compared against the installed distribution's version. It is
asked at most once every six hours (thirty minutes after a check that did not
complete), and the last-check time is stored at ~/.quickcode/update-check.json
so twenty launches in an afternoon are one request. A 403 carrying
x-ratelimit-remaining: 0 is honoured: nothing is asked again until the reset
time GitHub named.

Nothing is ever executed as a result. A pip or uv install is told the command,
because a process cannot reliably replace the package it is running. Only the
Windows installer layout is offered a download, and only after the release's
own SHA256SUMS.txt has vouched for the bytes -- a mismatch deletes the file and
refuses."""


def core_specs(*, prompt_view=None) -> list[PluginSpec]:
    """The internals that are not tools, agents, providers or MCP servers."""
    return [
        PluginSpec(
            id="runtime.tool_protocol",
            kind="policy",
            title="Tool call protocol",
            description="How tools are declared to the model and how calls come back.",
            group="Agent loop",
            required=True,
            summary="The handshake that makes every tool call reconstructible "
                    "from the session log.",
            affects=("tool_list", "loop", "storage"),
            audience="all_agents",
            consequence="Nothing about it varies between sessions or agents. The same "
                        "call always produces the same pair of records, which is what "
                        "lets a finished conversation be replayed and audited.",
            locked_because="The tool_call_id round trip is the join between what the "
                           "model asked for and what it got. A model that could invent "
                           "argument fields, or a result that came back untagged, would "
                           "produce a session the trajectory cannot reconstruct.",
            recourse=Recourse("docs", "Read the full protocol",
                              "docs/TOOLS.md#wire-format-note"),
            docs_anchor="docs/TOOLS.md#wire-format-note",
            settings=CORE_SETTINGS["runtime.tool_protocol"],
            view=lazy_view("text", _TOOL_PROTOCOL, "Tool call protocol"),
        ),
        PluginSpec(
            id="runtime.agent_loop",
            kind="hook",
            title="Agent loop",
            description="How many rounds a single turn may take before it stops.",
            group="Agent loop",
            required=True,
            summary="The per-turn budget: how many model-plus-tools rounds one "
                    "message may take.",
            affects=("loop",),
            audience="all_agents",
            consequence="At the budget the agent is told it is over the iteration "
                        "limit and asked to report state and next steps, so a stuck "
                        "turn ends with a handover rather than in silence.",
            docs_anchor="docs/ARCHITECTURE.md#the-agent-loop",
            settings=CORE_SETTINGS["runtime.agent_loop"],
        ),
        PluginSpec(
            id="runtime.compaction",
            kind="hook",
            title="Context compaction",
            description="Summarises the conversation when the context window fills up.",
            group="Agent loop",
            summary="Summarises a long conversation so it keeps fitting in the "
                    "context window.",
            affects=("loop",),
            audience="all_agents",
            consequence="When it runs, the older part of the conversation is replaced "
                        "by a summary and only the most recent turns survive word for "
                        "word. The agent keeps working but stops being able to quote "
                        "what it read earlier.",
            docs_anchor="docs/PROMPTS.md#4-compaction-prompt",
            settings=CORE_SETTINGS["runtime.compaction"],
        ),
        PluginSpec(
            id="runtime.permissions",
            kind="policy",
            title="Permissions",
            description="What the agent may do without asking you first.",
            group="Safety",
            required=True,
            summary="What the agent may do on its own, and what it has to stop and "
                    "ask about.",
            affects=("permissions",),
            audience="all_agents",
            consequence="The mode sets the default answer for anything that changes "
                        "something; the rules in settings.json decide the named cases. "
                        "Deny beats ask beats allow, in that order, every time.",
            locked_because="Rules match against the target a tool declares, so a path "
                           "resolving outside the project has no rule that could "
                           "honestly allow it. Without the boundary a single 'always "
                           "allow' answer would reach the whole filesystem.",
            recourse=Recourse("settings", "Open the project at a higher directory to "
                                          "widen the boundary", "runtime.permissions"),
            docs_anchor="docs/PERMISSIONS.md#modes",
            settings=CORE_SETTINGS["runtime.permissions"],
        ),
        PluginSpec(
            id="runtime.subagents",
            kind="hook",
            title="Subagents",
            description="Limits on how far the agent may fan work out.",
            group="Agent loop",
            summary="How far work may fan out, and what comes back when a subagent "
                    "finishes.",
            affects=("loop",),
            audience="all_agents",
            consequence="Both limits are counted per conversation rather than per "
                        "agent, so a wide fan-out near the top leaves less budget "
                        "further down. Hitting one raises an error the spawner sees "
                        "as a failed tool call.",
            locked_because="A subagent's report is text written by a model that may "
                           "have read files an attacker controls. It is neutralized "
                           "before it re-enters the spawner's context, and an off "
                           "switch for that is an off switch for the boundary between "
                           "the two agents.",
            recourse=Recourse("docs", "Read what the sanitizer rewrites",
                              "docs/AGENTS.md"),
            docs_anchor="docs/AGENTS.md",
            settings=CORE_SETTINGS["runtime.subagents"],
            view=lazy_view("text", _REPORT_SANITIZER, "Report sanitizer"),
        ),
        PluginSpec(
            id="hook.plan_mode",
            kind="hook",
            title="Plan mode",
            description="Withholds the mutating tools and routes plans to you for review.",
            group="Agent loop",
            required=True,
            tier_hint="locked",
            summary="In plan mode the mutating tools are not offered at all, only "
                    "described.",
            affects=("tool_list", "permissions", "loop"),
            audience="orchestrator",
            consequence="While the mode is plan, a tool that declares itself mutating "
                        "is dropped from the request and the plan tool takes its "
                        "place. Shell tools stay, because their read-only subcommands "
                        "are still worth having.",
            locked_because="A tool the model can see is a tool it will try. Offering "
                           "write and edit in plan mode and denying each call would "
                           "spend a round per attempt and teach the model that the "
                           "mode is advisory; withholding them is what makes it "
                           "structural.",
            recourse=Recourse("settings", "Leave plan mode to get the mutating tools "
                                          "back", "runtime.permissions"),
            docs_anchor="docs/PERMISSIONS.md#plan-mode",
            settings=CORE_SETTINGS["hook.plan_mode"],
        ),
        PluginSpec(
            id="runtime.session_log",
            kind="storage",
            title="Session log",
            description="The append-only record of every message and event.",
            group="Session",
            required=True,
            summary="The append-only file behind resume, the trajectory and every "
                    "replay.",
            affects=("storage", "ui"),
            audience="install",
            consequence="Everything a session did survives in one file per "
                        "conversation. Nothing rewrites it, so deleting the session "
                        "is the only way to remove what it recorded.",
            locked_because="Trajectory events carry a sequence number that only ever "
                           "goes up and is never reused, which is what lets a "
                           "reconnecting UI replay a session exactly once. A "
                           "rewritable log would make both resume and replay guesses.",
            recourse=Recourse("docs", "Read the record format in full",
                              "docs/ARCHITECTURE.md"),
            docs_anchor="docs/ARCHITECTURE.md",
            settings=CORE_SETTINGS["runtime.session_log"],
            view=lazy_view("text", _SESSION_FORMAT, "Session log format"),
        ),
        PluginSpec(
            id="runtime.updates",
            kind="policy",
            title="Update checking",
            description="Whether QuickCode asks github.com if a newer release exists.",
            group="Safety",
            summary="The one request this app makes to the internet on its own "
                    "initiative.",
            affects=("ui", "storage"),
            audience="install",
            consequence="On, a plain unauthenticated GET to the GitHub releases API "
                        "runs at most once every six hours and the answer is cached in "
                        "~/.quickcode. Off, nothing is sent and nothing is asked -- the "
                        "Install page then only reports the version you are running.",
            settings=CORE_SETTINGS["runtime.updates"],
            view=lazy_view("text", _UPDATE_CHECK, "Update check"),
        ),
        PluginSpec(
            id="prompt.system",
            kind="prompt_section",
            title="System prompt",
            description="The composed instructions every session starts from.",
            group="Prompt",
            required=True,
            summary="The composed instructions a session opens with, section by "
                    "section.",
            affects=("prompt",),
            audience="orchestrator",
            consequence="The sections below are joined in order with a blank line "
                        "between them, and a section that renders empty is dropped. "
                        "What you read here is the exact text this project's sessions "
                        "begin from.",
            locked_because="The prompt cache breakpoint sits on the system message, so "
                           "its bytes must stay stable inside a session. A rewrite "
                           "mid-conversation invalidates the cached prefix and the "
                           "next turn pays full price for the whole prompt again.",
            recourse=Recourse("settings", "Edit a section, then start a new session to "
                                          "pick it up", "prompt.tone"),
            docs_anchor="docs/PROMPTS.md#1-system-prompt-template",
            settings=CORE_SETTINGS["prompt.system"],
            view=prompt_view,
        ),
    ]
