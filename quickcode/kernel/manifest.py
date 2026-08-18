"""Internal plugins: everything QuickCode ships, declared as plugin specs.

These are derived from the live objects wherever possible -- tool specs come
from the registry the agent will actually use, agent specs from the loaded
definitions, MCP specs from the configured servers. That is deliberate: a
manifest that restated the runtime from memory would drift, and the Settings
UI would start describing an app that no longer exists.

Tiers here are the contract from ``docs/PLAN-PLUGIN-UI-OVERHAUL.md``. The
short version: how tools are called, how events are logged and how subagent
reports are sanitized are ``locked``; the knobs that move agent behaviour are
``confirm``; taste is ``free``.

**The prose is part of the manifest.** Every spec below answers the six
questions in ``kernel/spec.py`` -- what it is, what it affects, who it reaches,
what changes if you change it, and for the fixed ones why and what to do
instead. Two rules govern the writing:

* *Accuracy over fluency.* Every sentence here is checkable against the module
  it describes. A confident sentence that is wrong is worse than no sentence,
  so a field nobody could verify is left empty rather than filled.
* *Derived where derivable.* Tool and agent prose is computed from the tool's
  own ``PermissionSpec`` and the agent's ceiling and tool list, so a new tool
  gets a truthful card without anyone writing one. Only the facts that live in
  the wiring rather than in the object -- ``plan`` never reaching a subagent,
  the delegation pair being granted by depth -- are written by hand.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from quickcode.core.permissions import DEFAULT_SPEC, Mode
from quickcode.kernel.spec import (
    Audience,
    Effect,
    PluginSpec,
    PluginView,
    Recourse,
    SettingSpec,
)

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


def _view(fmt: str, content: str, title: str = "", path: str = ""):
    return lambda: PluginView(format=fmt, content=content, title=title, path=path)


# --------------------------------------------------------------------------
# Runtime internals
# --------------------------------------------------------------------------


_CORE_SETTINGS: dict[tuple[str, str], SettingSpec] | None = None


def core_setting(plugin_id: str, key: str) -> SettingSpec | None:
    """The declared spec for one internal setting, bounds included.

    The runtime resolves its limits through this rather than restating any of
    the numbers: the default the card shows, the minimum and the maximum are
    one declaration, so the value a reader is promised and the value the loop
    enforces cannot drift apart. It matters most for ``max_depth`` and
    ``max_agents``, whose declared maxima are safety backstops -- a stored
    setting is clamped to them, never able to raise them.
    """
    global _CORE_SETTINGS
    if _CORE_SETTINGS is None:
        _CORE_SETTINGS = {
            (spec.id, setting.key): setting
            for spec in core_specs()
            for setting in spec.settings
        }
    return _CORE_SETTINGS.get((plugin_id, key))


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
            settings=(
                SettingSpec(
                    key="strict_schemas", type="bool", default=True, tier="locked",
                    title="Strict argument schemas",
                    help="Tool arguments are validated against the declared schema; "
                         "unknown fields are rejected.",
                    affects=("tool_list",),
                    effect_detail="Each schema is generated from the tool's pydantic "
                                  "Input model with additionalProperties set to false, "
                                  "and the arguments are validated against that same "
                                  "model again before the tool runs.",
                ),
                SettingSpec(
                    key="parallel_read_only", type="bool", default=True, tier="confirm",
                    title="Run read-only tools in parallel",
                    help="Reads, globs and greps in one round run concurrently.",
                    risk="Turning this off makes every session slower. Turning it on "
                         "for tools that are not genuinely read-only can interleave "
                         "writes unpredictably.",
                    affects=("loop",),
                    effect_detail="Within one round the calls whose tool declares "
                                  "is_read_only are gathered and run together; every "
                                  "other call runs alone, in the order the model asked.",
                ),
            ),
            view=_view("text", _TOOL_PROTOCOL, "Tool call protocol"),
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
            settings=(
                SettingSpec(
                    key="max_rounds", type="int", default=50, tier="confirm",
                    minimum=1, maximum=500, title="Maximum rounds per turn",
                    help="A round is one model response plus the tools it asked for.",
                    risk="Raising this lets a confused agent burn tokens for much "
                         "longer before it gives up.",
                    affects=("loop",),
                    effect_detail="A round is one model response plus every tool it "
                                  "asked for. On the last round a wrap-up reminder is "
                                  "injected and the turn ends with the answer to it.",
                    example="80",
                ),
            ),
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
            settings=(
                SettingSpec(
                    key="enabled", type="bool", default=True, tier="free",
                    title="Compact automatically",
                    help="Off means long sessions end in a context-length error instead.",
                    affects=("loop",),
                    effect_detail="Off means nothing intervenes: a session that outgrows "
                                  "the window ends in a provider length error rather "
                                  "than in a summary.",
                ),
                SettingSpec(
                    key="threshold", type="float", default=0.8, tier="confirm",
                    minimum=0.3, maximum=0.98, title="Trigger at context fraction",
                    risk="Set too high, compaction runs too late to fit; too low and "
                         "the agent keeps losing detail it still needed.",
                    affects=("loop",),
                    effect_detail="The fraction of the model's context window the "
                                  "running token ledger has to cross before a summary "
                                  "is taken, checked between turns.",
                    example="0.7",
                ),
                SettingSpec(
                    key="keep_turns", type="int", default=2, tier="confirm",
                    minimum=0, maximum=20, title="Recent turns kept verbatim",
                    risk="Fewer kept turns means more of the recent work survives only "
                         "as summary.",
                    affects=("loop",),
                    effect_detail="How many user-started turns are carried through "
                                  "unchanged after the summary. The cut is made at a "
                                  "user message, so no tool call is separated from its "
                                  "result.",
                    example="4",
                ),
            ),
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
            settings=(
                SettingSpec(
                    key="default_mode", type="enum", default="ask", tier="confirm",
                    choices=("plan", "ask", "auto-edit", "dontask", "yolo"),
                    title="Default mode for new sessions",
                    risk="Anything past auto-edit lets the agent change files and run "
                         "commands without stopping to ask.",
                    affects=("permissions",),
                    effect_detail="The mode a new session opens in. It is a starting "
                                  "point, not a cap: a session is still clamped to the "
                                  "active preset's ceiling, and can be changed while "
                                  "it runs.",
                    example="auto-edit",
                ),
                SettingSpec(
                    key="protect_outside_root", type="bool", default=True, tier="locked",
                    title="Refuse writes outside the project",
                    help="Paths outside the project root, and .git, .quickcode, .env "
                         "and .ssh inside it, always prompt before any rule applies.",
                    affects=("permissions",),
                    effect_detail="Checked before the rule list: a path resolving "
                                  "outside the project root, or into .git, .quickcode, "
                                  ".ssh or a .env file, forces a prompt -- and a "
                                  "refusal outright wherever there is nobody to ask, "
                                  "which is dontask mode and every subagent.",
                ),
            ),
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
            settings=(
                SettingSpec(
                    key="max_depth", type="int", default=2, tier="confirm",
                    minimum=0, maximum=4, title="Maximum nesting depth",
                    risk="Deeper trees multiply cost fast and make a run hard to follow.",
                    affects=("loop",),
                    effect_detail="Depth 0 is the agent you talk to. At the limit the "
                                  "delegation tools are withheld from the child "
                                  "entirely, so a leaf agent cannot spawn at all.",
                    example="1",
                ),
                SettingSpec(
                    key="max_agents", type="int", default=50, tier="confirm",
                    minimum=1, maximum=500, title="Maximum agents per session",
                    risk="This is the backstop against a runaway fan-out loop.",
                    affects=("loop",),
                    effect_detail="Counted across the whole conversation, including "
                                  "agents that already finished. Spawning past it "
                                  "raises an error the spawner reads, rather than "
                                  "quietly doing nothing.",
                    example="20",
                ),
                SettingSpec(
                    key="sanitize_reports", type="bool", default=True, tier="locked",
                    title="Neutralize control tags in subagent output",
                    help="Control tags in a returning report are rewritten so a file "
                         "the child read cannot issue instructions to the spawner.",
                    affects=("loop",),
                    effect_detail="The tags system-reminder, task, objective, context "
                                  "and boundaries are rewritten to lookalike "
                                  "characters, and the report is prefixed so the "
                                  "spawner can see that it passed through.",
                ),
            ),
            view=_view("text", _REPORT_SANITIZER, "Report sanitizer"),
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
            settings=(
                SettingSpec(
                    key="withhold_mutating_tools", type="bool", default=True,
                    tier="locked", title="Hide mutating tools in plan mode",
                    help="A tool the model can see is a tool it will try, so in plan "
                         "mode the mutating tools are not offered at all. Shell tools "
                         "stay, gated per subcommand.",
                    affects=("tool_list",),
                    effect_detail="Filtered on the way into each request: a tool whose "
                                  "permission shape says it mutates, and which is not "
                                  "a shell tool, is left out of the schema list while "
                                  "the mode is plan.",
                ),
            ),
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
            settings=(
                SettingSpec(
                    key="format", type="string", default="jsonl", tier="locked",
                    title="On-disk format",
                    help="One JSON object per line, three record kinds interleaved.",
                    affects=("storage",),
                    effect_detail="One JSON object per line: message records for what "
                                  "the model saw, event records for what you saw, meta "
                                  "records for title, model and cwd. Appending is the "
                                  "only write.",
                ),
            ),
            view=_view("text", _SESSION_FORMAT, "Session log format"),
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
            settings=(
                SettingSpec(
                    key="check_automatically", type="bool", default=True, tier="free",
                    title="Check for updates automatically",
                    help="A plain unauthenticated GET of the GitHub releases API, at "
                         "most once every six hours. It carries no API key, no cookies, "
                         "no identifier, no project path, no session or usage data and "
                         "no version number -- there is no telemetry here. Off means "
                         "nothing is sent at all.",
                    affects=("ui", "storage"),
                    effect_detail="Off, no request is made -- not at launch, and not by "
                                  "the Check now button on Install > Updates, which "
                                  "does not quietly re-enable this. Any answer already "
                                  "stored is still shown, labelled as the last one that "
                                  "arrived.",
                ),
                SettingSpec(
                    key="endpoint", type="string",
                    default="https://api.github.com/repos/devincii-io/QuickCode"
                            "/releases/latest",
                    tier="locked", fact=True,
                    title="Where the check goes",
                    help="One address, unauthenticated. There is no fallback host and "
                         "no second endpoint.",
                    affects=("ui",),
                    effect_detail="The only outbound address QuickCode contacts without "
                                  "being asked. Everything else it sends goes to the "
                                  "model provider configured under Install.",
                    locked_because="An update check that could be pointed somewhere else "
                                   "is a channel for handing this app an installer to "
                                   "run. The address is fixed so the checksum it is "
                                   "verified against comes from the same release.",
                    recourse=Recourse("settings", "Switch the check off above to stop it "
                                                  "entirely", "runtime.updates"),
                ),
            ),
            view=_view("text", _UPDATE_CHECK, "Update check"),
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
            settings=(
                SettingSpec(
                    key="cache_stable", type="bool", default=True, tier="locked",
                    title="Byte-stable within a session",
                    help="The prompt cache breakpoint sits on this message, so it must "
                         "not change mid-session.",
                    affects=("prompt",),
                    effect_detail="The system message is sent with cache_control set, "
                                  "so the provider caches the prefix. Changing it "
                                  "mid-session drops that cache; edits therefore take "
                                  "effect in the next session.",
                ),
            ),
            view=prompt_view,
        ),
    ]


# --------------------------------------------------------------------------
# Prompt sections
# --------------------------------------------------------------------------

# Every section reaches the orchestrator's system prompt and nothing else:
# subagents are rendered from the separate template in ``prompts/subagent.py``
# and never see these blocks. That is a fact about today's wiring, so it is
# stated once here rather than repeated per row.
_SECTION_AUDIENCE: Audience = "orchestrator"

# A replacement body is returned verbatim by ``PromptSection.body``: it is not
# formatted and the render function's own conditions are not consulted. For the
# four sections that render conditionally, that is a real surprise, so each one
# says so.
_OVERRIDE_UNCONDITIONAL = (
    "A replacement body is used unconditionally, so text written here appears "
    "even in the sessions where the default renders nothing."
)

# id -> (summary, consequence, effect_detail on the body setting)
_SECTION_PROSE: dict[str, tuple[str, str, str]] = {
    "prompt.identity": (
        "Tells the agent it is QuickCode and which model is answering right now.",
        "This is the line the agent answers identity questions from, including "
        "after a model switch mid-conversation. Rewriting it changes what it "
        "says it is, not what it can do.",
        "Rendered with the live model and provider names. A replacement body is "
        "used verbatim, so placeholders written into your own text are not "
        "substituted.",
    ),
    "prompt.tone": (
        "How replies read: length, no preamble, no narrating routine tool calls.",
        "Governs the prose in the chat pane and nothing else. Loosening it costs "
        "reading time, not correctness.",
        "Instruction text only. It says nothing about which tools are used or "
        "when, so a change here cannot alter what the agent does.",
    ),
    "prompt.autonomy": (
        "How far the agent goes on its own before it stops to ask you.",
        "Widening it means fewer questions and more unasked-for work; narrowing "
        "it means the agent stops for confirmations it could have inferred.",
        "Instruction text only. The permission engine decides what is actually "
        "allowed, so this changes what the agent attempts, not what it can do.",
    ),
    "prompt.conventions": (
        "Codebase manners: match local style, check imports, no unasked comments.",
        "Drop it and the agent is no longer told to read neighbouring files "
        "first, so new code matches its own habits rather than the project's.",
        "Instruction text only. Nothing here is enforced by a tool; it is read "
        "once per session along with the rest of the prompt.",
    ),
    "prompt.task_management": (
        "When work goes on the task board instead of being improvised in place.",
        "Remove it and the task tools stay available but go mostly unused, so "
        "multi-step work leaves no visible plan behind.",
        "Only meaningful while the task tools are in the agent's tool list. The "
        "text names them; it does not grant them.",
    ),
    "prompt.tool_use_policy": (
        "The rules for choosing, batching and paginating tool calls.",
        "It names this machine's shell and platform, so the commands the agent "
        "writes are the ones the local shell will actually accept.",
        "Rendered with this machine's shell and platform. Batching, pagination "
        "and read-before-edit are the half of the tool contract the model holds "
        "up.",
    ),
    "prompt.verification": (
        "Tells the agent to run the project's checks before calling work done.",
        "Without it the agent can still run tests but is no longer asked to, so "
        "\"done\" starts to mean \"written\".",
        "Instruction text only. It does not run anything and does not know which "
        "test command this project uses.",
    ),
    "prompt.environment": (
        "The session's facts: directory, platform, shell, date and git branch.",
        "Read from the machine when the session opens. It is what the agent "
        "knows about where it is running without spending a tool call.",
        "Generated from the live environment at session open; there is no "
        "template text behind it to edit.",
    ),
    "prompt.project_instructions": (
        "Your project's own QUICKCODE.md, AGENTS.md or CLAUDE.md, quoted in full.",
        "This is the one part of the prompt you change by editing a file in the "
        "repository rather than a setting in here.",
        "Filled from the first instructions file found in the project, with the "
        "source path recorded in the block's opening tag.",
    ),
    "prompt.orchestration": (
        "The delegation playbook: when to spawn, how many, and how to word a task.",
        "Present only when the session has the delegation tools. Take those away "
        "and the section renders empty and is dropped from the prompt entirely.",
        "Rendered only when the session can spawn subagents. " + _OVERRIDE_UNCONDITIONAL,
    ),
    "prompt.send_message_hint": (
        "Reminds the agent a finished subagent can be resumed instead of respawned.",
        "Without it the agent tends to spawn a fresh subagent for a follow-up, "
        "losing the first one's context and paying for the same work twice.",
        "Rendered only when the session can spawn subagents. " + _OVERRIDE_UNCONDITIONAL,
    ),
    "prompt.plan_mode": (
        "Added while the session is in plan mode: investigate, design, change nothing.",
        "Appears and disappears as you switch modes. The mutating tools are "
        "withheld by the plan-mode hook whether or not this text is present.",
        "Rendered only while the mode is plan. " + _OVERRIDE_UNCONDITIONAL,
    ),
    "prompt.headless": (
        "Added for non-interactive runs: never ask, the last message is the output.",
        "Present only in headless runs, where there is nobody to answer a "
        "question and an unanswered prompt would hang the process.",
        "Rendered only for headless runs. " + _OVERRIDE_UNCONDITIONAL,
    ),
}

# Why a section's body cannot be replaced, for the ones that cannot.
_SECTION_LOCKED: dict[str, tuple[str, Recourse]] = {
    "prompt.tool_use_policy": (
        "Batching, pagination and read-before-edit are the half of the tool "
        "contract the model holds up. Rewriting them locally produces calls that "
        "read correctly in the transcript and behave differently in the loop.",
        Recourse("author", "Write your own section to run after this one",
                 "prompt_section"),
    ),
    "prompt.environment": (
        "These are observations, not instructions. A hand-edited environment "
        "block would tell the agent it is on a platform it is not, and every "
        "shell command written after that would use the wrong syntax.",
        Recourse("settings", "Open the project elsewhere to change these facts",
                 "prompt.system"),
    ),
    "prompt.project_instructions": (
        "The body is read from the project's instructions file when the session "
        "opens. Editing the copy shown here would create a second source of "
        "truth that the next session silently overwrites.",
        Recourse("docs", "Edit the project's instructions file instead",
                 "docs/PROMPTS.md#1-system-prompt-template"),
    ),
}

_GENERIC_SECTION_RECOURSE = Recourse(
    "author", "Write your own section to run after this one", "prompt_section"
)


def prompt_section_specs(bodies: dict[str, str] | None = None) -> list[PluginSpec]:
    """One plugin per system-prompt section.

    ``bodies`` carries each section's rendered text for this project, so the
    UI shows what the agent is actually being told rather than a template.
    Sections that render conditionally (plan mode, headless) are listed even
    when inactive -- being able to read them is the point.
    """
    from quickcode.prompts import sections as prompt_sections

    rendered = bodies or {}
    out: list[PluginSpec] = []
    for section in prompt_sections.ordered():
        body = rendered.get(section.id, "")
        editable = section.tier != "locked" and not section.generated
        summary, consequence, effect_detail = _SECTION_PROSE.get(
            section.id, (section.description, "", "")
        )
        locked_because, recourse = _SECTION_LOCKED.get(
            section.id, ("", _GENERIC_SECTION_RECOURSE)
        )
        # The setting exists either way, locked when the section cannot be
        # rewritten: "you may not change this" is a far better answer than
        # "no such setting" for something the reader can plainly see.
        settings = (
            SettingSpec(
                key="body", type="text", default=body,
                tier=section.tier if editable else "locked",
                title="Section text",
                help=section.description,
                risk="This text is part of every turn's instructions. Rewriting "
                     "it changes how the agent behaves for the whole session.",
                affects=("prompt",),
                effect_detail=effect_detail,
                locked_because=locked_because,
                recourse=recourse if not editable else None,
            ),
        )
        out.append(PluginSpec(
            id=section.id,
            kind="prompt_section",
            title=section.title,
            description=section.description,
            group="Prompt",
            required=not editable,
            settings=settings,
            # A section whose body cannot be replaced reads as locked, whatever
            # its declared tier: the badge has to match the only knob it has.
            tier_hint=section.tier if editable else "locked",
            summary=summary,
            affects=("prompt",),
            audience=_SECTION_AUDIENCE,
            consequence=consequence,
            locked_because=locked_because,
            recourse=recourse if not editable else None,
            docs_anchor="docs/PROMPTS.md#1-system-prompt-template",
            metadata={
                "order": section.order,
                "generated": section.generated,
                "active": bool(body),
                "editable": editable,
                "edit_hint": (
                    "Generated from this session's facts and the project's "
                    "instructions file — edit those, not this."
                    if section.generated else ""
                ),
            },
            view=_view("text", body or "(not part of this session's prompt)",
                       section.title),
        ))
    return out


# --------------------------------------------------------------------------
# Derived from live objects
# --------------------------------------------------------------------------


def _tool_group(tool: Any) -> str:
    name = getattr(tool, "name", "")
    if name.startswith("mcp__"):
        return "MCP"
    if name in ("bash",):
        return "Shell"
    if name in ("read", "write", "edit", "glob", "grep"):
        return "Files"
    if name in ("web_fetch", "web_search"):
        return "Web"
    if name.startswith("task"):
        return "Tasks"
    if name in ("agent", "send_message"):
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
                       "child then does.",
    },
    "send_message": {
        "affects": ("tool_list", "loop"),
        "summary": "Resumes a finished subagent instead of spawning a fresh one.",
        "consequence": "Granted by depth alongside the spawn tool. The resumed agent "
                       "keeps its full context, which is why this is cheaper than "
                       "spawning the same work again.",
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
    "web_fetch": "docs/TOOLS.md#web_fetch",
    "web_search": "docs/TOOLS.md#web_search",
    "agent": "docs/TOOLS.md#agentic-tools-specced-in-docsagentsmd-and-docspermissionsmd",
    "send_message": "docs/TOOLS.md#agentic-tools-specced-in-docsagentsmd-and-docspermissionsmd",
    "plan": "docs/PERMISSIONS.md#plan-mode",
}

_MCP_NOTE = (
    " It comes from an external server process, so it disappears from every "
    "agent's tool list the moment that server is removed."
)

_READ_ONLY_LOCKED_BECAUSE = (
    "Parallelism and the default permission answer are both read off this flag "
    "on every call. A tool that mutated while claiming to be read-only would "
    "run concurrently with a write and skip the prompt, so the tool declares it "
    "in code and nothing outside the tool can override it."
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

_READ_ONLY_RECOURSE = Recourse(
    "settings", "Gate this tool with a permission rule instead", "runtime.permissions"
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

        try:
            schema = tool.schema()
            payload = json.dumps(
                {"name": schema.name, "description": schema.description,
                 "parameters": schema.parameters},
                indent=2,
            )
        except Exception as exc:
            payload = f"schema unavailable: {exc}"

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
                    locked_because=_READ_ONLY_LOCKED_BECAUSE,
                    recourse=_READ_ONLY_RECOURSE,
                    # Declared by the tool class, not withheld from you: this
                    # row reports a fact and must not badge the whole card
                    # locked. See PluginSpec.tier.
                    fact=True,
                ),
            ),
            path=getattr(tool, "path", "") or "",
            metadata={"tool_name": name,
                      "read_only": read_only,
                      "character": _tool_character(tool)},
            view=_view("json", payload, f"{name} schema",
                       getattr(tool, "path", "") or ""),
        ))
    return out


# -- agent prose, derived from the definition's ceiling and tool list ---------

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


def agent_specs(defs: dict[str, Any]) -> list[PluginSpec]:
    """One plugin per subagent definition -- built-in and user-authored alike."""
    out: list[PluginSpec] = []
    for name, defn in sorted(defs.items()):
        builtin = name in ("explore", "general")
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
            view=_view("markdown", getattr(defn, "prompt_body", "") or "",
                       f"{name} instructions"),
        ))
    return out


# --------------------------------------------------------------------------
# Authored plugins
# --------------------------------------------------------------------------

# Why an authored plugin has no locked settings and nothing required: the tier
# system protects QuickCode's internals from you, and it does not protect your
# own files from you. Tier is a property of a plugin's *source*, not of its
# content -- a duplicated copy of a locked agent is free down to the byte.

_AUTHORED_TOOL_LOCKED = (
    "A command tool is executed as an argv array, never through a shell, so a "
    "parameter value can never become two arguments. That shape is what makes "
    "the tool safe to hand a model, and it is not a knob."
)

_AUTHORED_TOOL_RECOURSE = Recourse(
    "author", "Edit the file to change the command", "tool",
)

_READ_ONLY_CLAIM_HELP = (
    "Recorded from the file and honoured by nothing. QuickCode cannot check "
    "what a program does, so a declaration here never removes the permission "
    "prompt -- a file that could opt itself out of being asked about would be "
    "the same hole as an unaudited external server. To stop being asked, add a "
    "permission rule naming this tool."
)


def _authored_tool_spec(plugin: Any, tool: Any) -> PluginSpec:
    try:
        schema = tool.schema()
        payload = json.dumps(
            {"name": schema.name, "description": schema.description,
             "parameters": schema.parameters},
            indent=2,
        )
    except Exception as exc:
        payload = f"schema unavailable: {exc}"

    argv = " ".join(plugin.argv)
    scope = "this project" if plugin.scope == "project" else "every project"
    return PluginSpec(
        id=plugin.id,
        kind="tool",
        title=plugin.name,
        description=plugin.description,
        group=plugin.group or "Yours",
        source="authored",
        path=plugin.path,
        derived_from=plugin.derived_from,
        enabled_by_default=plugin.enabled_by_default,
        summary=f"Runs {plugin.argv[0] if plugin.argv else 'a command'}; "
                f"yours, from a file."[:90],
        affects=("tool_list", "permissions"),
        audience="all_agents",
        consequence=(
            f"Available in {scope}. It runs `{argv}` as an argument array with "
            "no shell involved, so a parameter value is one argument whatever "
            "it contains. Every call goes through the permission gate like any "
            "other mutating tool."
        ),
        locked_because=_AUTHORED_TOOL_LOCKED,
        recourse=_AUTHORED_TOOL_RECOURSE,
        docs_anchor="docs/design/AUTHORING.md",
        settings=(
            SettingSpec(
                key="read_only", type="bool", default=False, tier="locked",
                title="Read-only", help=_READ_ONLY_CLAIM_HELP,
                affects=("permissions",),
                effect_detail="A command tool always declares itself mutating, "
                              "so it is withheld in plan mode and prompted for "
                              "in ask mode.",
                locked_because=_READ_ONLY_LOCKED_BECAUSE,
                recourse=_READ_ONLY_RECOURSE,
                fact=True,
            ),
        ),
        metadata={
            "tool_name": plugin.name,
            "authored": True,
            "scope": plugin.scope,
            "argv": list(plugin.argv),
            "read_only_declared": plugin.read_only_declared,
            "read_only": False,
            "timeout_ms": plugin.timeout_ms,
            "output": plugin.output,
            "params": [p.name for p in plugin.params],
        },
        view=_view("json", payload, f"{plugin.name} schema", plugin.path),
    )


def _authored_prompt_spec(plugin: Any) -> PluginSpec:
    scope = "this project" if plugin.scope == "project" else "every project"
    when = plugin.when
    condition = {
        "always": "in every session",
        "plan": "only while the session is in plan mode",
        "orchestration": "only when the session can spawn subagents",
        "headless": "only in non-interactive runs",
    }.get(when, "in every session")
    return PluginSpec(
        id=plugin.id,
        kind="prompt_section",
        title=plugin.display_title,
        description=plugin.description or "An authored prompt section.",
        group=plugin.group or "Prompt",
        source="authored",
        path=plugin.path,
        derived_from=plugin.derived_from,
        enabled_by_default=plugin.enabled_by_default,
        summary=f"Your own block of the system prompt, at order {plugin.order}."[:90],
        affects=("prompt",),
        audience="orchestrator",
        consequence=(
            f"Added to the composed system prompt {condition}, in {scope}, at "
            f"order {plugin.order}. Edits take effect in the next session: the "
            "prompt cache breakpoint sits on the system message, so its bytes "
            "must stay stable inside a session."
        ),
        docs_anchor="docs/design/AUTHORING.md",
        settings=(
            SettingSpec(
                key="body", type="text", default=plugin.prose, tier="free",
                title="Section text",
                help="The text this section contributes, verbatim.",
                affects=("prompt",),
                effect_detail="Composed with the internal sections in order, "
                              "joined by a blank line. A section that renders "
                              "empty is dropped.",
            ),
        ),
        metadata={
            "order": plugin.order,
            "authored": True,
            "scope": plugin.scope,
            "applies_to": list(plugin.applies_to),
            "when": when,
            "generated": False,
            "active": True,
            "editable": True,
        },
        view=_view("markdown", plugin.prose, plugin.display_title, plugin.path),
    )


def authored_specs(plugins: Any, tools: dict[str, Any] | None = None) -> list[PluginSpec]:
    """One spec per authored plugin, every setting free, nothing required.

    ``agent`` files are absent on purpose: they arrive through ``load_defs``
    and are rendered by ``agent_specs`` like every other definition, which is
    the path agents were already on. Adding a second branch for them here would
    mean two specs for one file and a rule about which wins.
    """
    built = tools or {}
    out: list[PluginSpec] = []
    for plugin in plugins:
        try:
            if plugin.kind == "tool":
                tool = built.get(plugin.id) or plugin.to_tool()
                out.append(_authored_tool_spec(plugin, tool))
            elif plugin.kind == "prompt":
                out.append(_authored_prompt_spec(plugin))
        except Exception:  # one broken file must not hide the others
            continue
    return out


def provider_specs(factories: dict[str, Any], *, active: str = "") -> list[PluginSpec]:
    out: list[PluginSpec] = []
    for name in sorted(factories):
        is_active = name == active
        out.append(PluginSpec(
            id=f"provider.{name}",
            kind="provider",
            title=name,
            description="Model backend." if name != "openai-compat"
                        else "Any OpenAI-compatible endpoint, including OpenRouter.",
            group="Models",
            source="internal" if name == "openai-compat" else "entrypoint",
            summary="Supplies the models every agent runs on, for the whole install.",
            affects=("models",),
            audience="install",
            consequence=(
                "This is the backend every session currently talks to: it decides "
                "which model slugs exist, what they cost and where the API key goes."
                if is_active else
                "Not the active backend. Switching to it changes which model slugs "
                "exist and which key is used, for every project on this machine."
            ),
            docs_anchor="docs/ARCHITECTURE.md#provider-layer",
            metadata={"provider": name, "active": is_active},
        ))
    return out


def mcp_specs(configs: dict[str, dict[str, Any]]) -> list[PluginSpec]:
    out: list[PluginSpec] = []
    for name, cfg in sorted(configs.items()):
        command = " ".join([str(cfg.get("command", ""))] + list(cfg.get("args", []))).strip()
        out.append(PluginSpec(
            id=f"mcp.{name}",
            kind="mcp_server",
            title=name,
            description=command or "External MCP server.",
            group="MCP",
            source="config",
            summary="An external process whose tools join this project's tool pool.",
            affects=("tool_list",),
            audience="all_agents",
            consequence="Started once per QuickCode run, not per session. Its tools "
                        "are named mcp__<server>__<tool> and go through the same "
                        "permission gate as the built-ins; a tool that does not "
                        "declare itself read-only is prompted for.",
            docs_anchor="docs/ARCHITECTURE.md#the-plugin-kernel",
            metadata={"server": name, "command": cfg.get("command", ""),
                      "args": list(cfg.get("args", []))},
            view=_view("json", json.dumps(cfg, indent=2), f"{name} definition"),
        ))
    return out
