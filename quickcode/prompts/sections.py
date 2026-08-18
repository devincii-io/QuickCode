"""The system prompt as an ordered set of sections.

It used to be one format string with ``if`` flags deciding what got appended.
That worked, but nothing could see inside it: the UI could not say which part
of the prompt came from where, and nothing could be overridden without editing
Python. Each block is now a section with an id, an order, a tier and a
renderer, and the prompt is what you get when you compose them.

Two rules the composition must keep:

* **Byte-stability within a session.** The cache breakpoint sits on the system
  message (``core/history.py``), so the same inputs must produce the same
  bytes. Sections are joined with a blank line, in ``order``, and a section
  that renders empty is dropped entirely -- exactly what the old template did.
* **The tool-use policy is locked.** How tools are called is not a matter of
  taste; it is the contract the loop and the trajectory depend on. It is
  visible, like everything else, but not editable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from quickcode.config import Environment
from quickcode.kernel.spec import Tier

SEPARATOR = "\n\n"


@dataclass(frozen=True)
class PromptContext:
    """Everything a section may depend on. Frozen for the session."""

    env: Environment
    model: str = "an unknown model"
    provider: str = "the configured provider"
    headless: bool = False
    plan: bool = False
    orchestration: bool = False
    # section id -> replacement body, from the plugin registry. A section not
    # named here renders its default.
    overrides: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PromptSection:
    id: str
    title: str
    order: int
    tier: Tier
    render: Callable[[PromptContext], str]
    description: str = ""
    # True when the body is generated from session facts rather than authored
    # prose -- the UI shows it read-only even at a permissive tier.
    generated: bool = False

    def body(self, ctx: PromptContext) -> str:
        override = ctx.overrides.get(self.id)
        if override is not None and self.tier != "locked" and not self.generated:
            return override.strip()
        return self.render(ctx).strip()


@dataclass(frozen=True)
class RenderedSection:
    """One section's contribution, with where it landed in the final text."""

    id: str
    title: str
    tier: Tier
    text: str
    start: int
    end: int


# --------------------------------------------------------------------------
# The sections themselves. Text is unchanged from the original template.
# --------------------------------------------------------------------------

_IDENTITY = """\
<identity>
You are QuickCode, a local coding agent with a web interface. You help the
user with software engineering: fixing bugs, implementing features,
refactoring, explaining code, and running project commands.

You are powered by the model "{model}" served through {provider} (an
OpenAI-compatible endpoint). When asked which model or agent you are, answer
plainly with that: you are QuickCode running on {model}. This line is
authoritative and live — answer identity questions from it directly, without
running commands or reading config files to "verify" it. If earlier messages
in the conversation name a different model, the user switched models
mid-conversation and this line reflects the current one. QuickCode can drive
different underlying models, so do not assume any capability or vendor beyond
what "{model}" implies.
</identity>"""

_TONE = """\
<tone_and_style>
Your output renders as markdown in the chat pane. Be concise and direct.

- Answer simple questions in 1-4 lines. No preamble ("Great question!"),
  no postamble ("Let me know if..."), no restating the question.
- After completing a task, summarize in 1-3 sentences what changed and how
  you verified it. Do not paste large code blocks you just wrote via tools.
- Never narrate routine tool use ("Now I'll read the file..."). Only write
  text between tool calls when you found something important or changed
  direction — one sentence.
- Reference code as file_path:line so the user can jump to it.
</tone_and_style>"""

_AUTONOMY = """\
<autonomy>
- Do what was asked; nothing more. A bug fix does not need surrounding
  cleanup. Don't add abstractions, error handling, or features that were
  not requested.
- For minor choices (naming, formatting, equivalent approaches), pick a
  reasonable option and note it rather than asking.
- Stop and ask before: destructive or hard-to-reverse actions, actions
  outside the project directory, and genuine scope changes.
- When the user is asking a question or describing a problem, the
  deliverable is your assessment — investigate and report. Do not apply
  fixes until asked.
</autonomy>"""

_CONVENTIONS = """\
<conventions>
- Before writing code, look at neighboring files to match the codebase's
  existing style, naming, and idioms. Mimic, don't impose.
- Never assume a library is available: check package.json / imports /
  lockfiles first.
- Follow security best practice: never log or commit secrets, never
  introduce code that exposes keys.
- Do not add code comments unless asked, or the code is genuinely
  non-obvious. Never write comments that talk to the reviewer.
- Never commit unless the user explicitly asks.
</conventions>"""

_TASK_MANAGEMENT = """\
<task_management>
Use the todo/task tools to plan multi-step work (3+ distinct steps) and mark
progress as you go: exactly one task in_progress at a time, mark completed
immediately when done — don't batch completions. Skip the task list for
trivial single-step tasks; using it there is noise.
</task_management>"""

_TOOL_USE_POLICY = """\
<tool_use_policy>
- Batch independent tool calls in a single response — e.g. read three
  files with three parallel read calls, not sequentially.
- Prefer grep/glob for searching. Never use bash find/grep/cat/ls when a
  dedicated tool exists — dedicated tools are faster and paginated.
- Prefer edit over write for existing files. write is for new files only.
- You must read a file before editing it.
- Bash runs {shell_name} on {platform}. Write {shell_name} syntax.
- If a tool result is truncated, use its pagination parameters (offset,
  limit) to fetch the part you need — do not re-run the same call.
</tool_use_policy>"""

_VERIFICATION = """\
<verification>
After changing code, verify it: run the project's tests, typechecker, or
linter if they exist (check package.json scripts / Makefile). Report
results honestly — if tests fail, say so with the output. Never claim
untested work is working.
</verification>"""

_ENVIRONMENT = """\
<environment>
  <cwd>{cwd}</cwd>
  <platform>{platform}</platform>
  <os_version>{os_version}</os_version>
  <shell>{shell_name}</shell>
  <date>{session_date}</date>
  <is_git_repo>{is_git_repo}</is_git_repo>
  <git_branch>{git_branch}</git_branch>
</environment>"""

_PROJECT_INSTRUCTIONS = """\
<project_instructions source="{instructions_file}">
{project_instructions}
</project_instructions>"""

_SEND_MESSAGE_HINT = """\
<send_message_hint>
A finished subagent stays resumable: call send_message with its returned id to
continue the same task with its full context intact, instead of respawning a
fresh subagent.
</send_message_hint>"""

_PLAN_MODE = """\
<plan_mode>
You are in PLAN MODE. Investigate and design; do not mutate anything. The
editing and mutating tools are withheld. When you have a complete plan, call
the plan tool with the plan as markdown. Do not attempt to implement yet.
</plan_mode>"""

_HEADLESS = """\
<headless_mode>
You are running non-interactively. There is no user to answer questions —
never ask; choose the reasonable option and proceed. Your final message is
the program's entire output: lead with the result.
</headless_mode>"""


def _identity(ctx: PromptContext) -> str:
    return _IDENTITY.format(model=ctx.model, provider=ctx.provider)


def _tool_use_policy(ctx: PromptContext) -> str:
    return _TOOL_USE_POLICY.format(
        shell_name=ctx.env.shell_name, platform=ctx.env.platform
    )


def _environment(ctx: PromptContext) -> str:
    env = ctx.env
    return _ENVIRONMENT.format(
        cwd=env.cwd,
        platform=env.platform,
        os_version=env.os_version,
        shell_name=env.shell_name,
        session_date=env.session_date,
        is_git_repo=str(env.is_git_repo).lower(),
        git_branch=env.git_branch or "(none)",
    )


def _project_instructions(ctx: PromptContext) -> str:
    return _PROJECT_INSTRUCTIONS.format(
        instructions_file=ctx.env.instructions_file or "none",
        project_instructions=ctx.env.project_instructions.strip(),
    )


def _orchestration(ctx: PromptContext) -> str:
    if not ctx.orchestration:
        return ""
    from quickcode.prompts.subagent import ORCHESTRATION

    return ORCHESTRATION


def _send_message_hint(ctx: PromptContext) -> str:
    return _SEND_MESSAGE_HINT if ctx.orchestration else ""


def _plan_mode(ctx: PromptContext) -> str:
    return _PLAN_MODE if ctx.plan else ""


def _headless(ctx: PromptContext) -> str:
    return _HEADLESS if ctx.headless else ""


SECTIONS: list[PromptSection] = [
    PromptSection("prompt.identity", "Identity", 10, "confirm", _identity,
                  "Who the agent is and which model is answering."),
    PromptSection("prompt.tone", "Tone and style", 20, "free", lambda _: _TONE,
                  "How replies read: length, preamble, narration."),
    PromptSection("prompt.autonomy", "Autonomy", 30, "confirm", lambda _: _AUTONOMY,
                  "How far the agent goes before stopping to ask."),
    PromptSection("prompt.conventions", "Conventions", 40, "free", lambda _: _CONVENTIONS,
                  "Codebase manners: match local style, no unasked comments."),
    PromptSection("prompt.task_management", "Task management", 50, "free",
                  lambda _: _TASK_MANAGEMENT, "When to use the task board."),
    PromptSection("prompt.tool_use_policy", "Tool use policy", 60, "locked",
                  _tool_use_policy,
                  "How tools are chosen and called. Part of the contract the "
                  "loop and the trajectory depend on."),
    PromptSection("prompt.verification", "Verification", 70, "free",
                  lambda _: _VERIFICATION, "Checking work before claiming it done."),
    PromptSection("prompt.environment", "Environment", 80, "locked", _environment,
                  "Session facts: directory, platform, shell, branch, date.",
                  generated=True),
    PromptSection("prompt.project_instructions", "Project instructions", 90, "free",
                  _project_instructions,
                  "QUICKCODE.md / AGENTS.md / CLAUDE.md from the project.",
                  generated=True),
    PromptSection("prompt.orchestration", "Orchestration", 100, "confirm", _orchestration,
                  "The delegation playbook, when subagent tools are present."),
    PromptSection("prompt.send_message_hint", "Resume hint", 110, "confirm",
                  _send_message_hint, "Reminds the agent that subagents are resumable."),
    PromptSection("prompt.plan_mode", "Plan mode", 120, "confirm", _plan_mode,
                  "Added while the session is in plan mode."),
    PromptSection("prompt.headless", "Headless mode", 130, "confirm", _headless,
                  "Added for non-interactive runs."),
]


def ordered() -> list[PromptSection]:
    return sorted(SECTIONS, key=lambda s: s.order)


def get(section_id: str) -> PromptSection | None:
    for section in SECTIONS:
        if section.id == section_id:
            return section
    return None


def compose(ctx: PromptContext) -> tuple[str, list[RenderedSection]]:
    """Render the prompt and report where each section landed.

    The offsets are what lets the UI highlight "this run of bytes came from
    the tone section" instead of showing an undifferentiated wall of text.
    """
    parts: list[str] = []
    rendered: list[RenderedSection] = []
    cursor = 0
    for section in ordered():
        text = section.body(ctx)
        if not text:
            continue
        if parts:
            cursor += len(SEPARATOR)
        parts.append(text)
        rendered.append(RenderedSection(
            id=section.id, title=section.title, tier=section.tier,
            text=text, start=cursor, end=cursor + len(text),
        ))
        cursor += len(text)
    return SEPARATOR.join(parts), rendered
