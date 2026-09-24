"""The settings QuickCode's own internals declare, bounds included.

The loop, the compactor and the spawner read their limits from here, and the
Settings cards in ``kernel/manifest/core.py`` render the same objects. They
live apart from the card prose so the resolver depends on the declared numbers
alone, not on the description of every tool, agent and prompt section.
"""

from __future__ import annotations

from quickcode.kernel.spec import Recourse, SettingSpec

# plugin id -> the settings its card lists, in order.
CORE_SETTINGS: dict[str, tuple[SettingSpec, ...]] = {
    "runtime.tool_protocol": (
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
    "runtime.agent_loop": (
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
    "runtime.compaction": (
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
    "runtime.permissions": (
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
    "runtime.subagents": (
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
            key="max_parallel", type="int", default=4, tier="confirm",
            minimum=1, maximum=16, title="Maximum background jobs at once",
            risk="Every job in flight is a model running at full cost, "
                 "and none of them is visible in the turn that started it.",
            affects=("loop",),
            effect_detail="Applies to agent(background=true) only. A "
                          "blocking delegation is bounded by the number of "
                          "calls in one model turn; a detached one is not, "
                          "so this is the ceiling on how many run together. "
                          "Asking past it is an error the spawner reads, "
                          "never a queue it waits in.",
            example="2",
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
    "hook.plan_mode": (
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
    "runtime.session_log": (
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
    "runtime.updates": (
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
    "prompt.system": (
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
}

_BY_KEY: dict[tuple[str, str], SettingSpec] = {
    (plugin_id, setting.key): setting
    for plugin_id, settings in CORE_SETTINGS.items()
    for setting in settings
}


def core_setting(plugin_id: str, key: str) -> SettingSpec | None:
    """The declared spec for one internal setting, bounds included.

    The runtime resolves its limits through this rather than restating any of
    the numbers: the default the card shows, the minimum and the maximum are
    one declaration, so the value a reader is promised and the value the loop
    enforces cannot drift apart. It matters most for ``max_depth`` and
    ``max_agents``, whose declared maxima are safety backstops -- a stored
    setting is clamped to them, never able to raise them.
    """
    return _BY_KEY.get((plugin_id, key))
