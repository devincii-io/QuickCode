"""QuickCode CLI entry point.

Parses arguments, assembles the agent (provider, registry, permissions,
history), and either runs one headless turn (``-p/--print``) or launches the
Textual TUI.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from quickcode.config import Config, Environment
from quickcode.core.agent import AgentInstance, PermissionOutcome, PermissionRequest
from quickcode.core.history import History
from quickcode.core.permissions import Mode, PermissionEngine, Rules
from quickcode.prompts.system import render_system_prompt
from quickcode.providers.openai_compat import OpenAICompatProvider
from quickcode.tools.base import ReadRegistry, ToolCtx
from quickcode.tools.registry import default_registry

try:
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("quickcode")
except Exception:  # not installed as a package (running from source)
    __version__ = "0.1.0-dev"


async def _headless_permission_cb(request: PermissionRequest) -> PermissionOutcome:
    return PermissionOutcome(allow=False, deny_message="headless: not permitted")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quickcode", description="QuickCode coding agent")
    parser.add_argument("prompt", nargs="?", default=None, help="initial prompt (optional)")
    parser.add_argument("-p", "--print", dest="print_mode", action="store_true",
                         help="run headlessly: print the final response and exit")
    parser.add_argument("--cwd", default=None, help="project directory (default: current dir)")
    parser.add_argument("--mode", default=None,
                         choices=[m.value for m in Mode],
                         help="starting permission mode")
    parser.add_argument("--model", default=None, help="override the orchestrator model")
    parser.add_argument("--yolo", action="store_true",
                         help="allow cycling into yolo mode (skips all permission prompts)")
    parser.add_argument("--continue", dest="continue_session", action="store_true",
                         help="continue the most recent session (not yet persisted)")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    return parser


def _build_agent(args: argparse.Namespace):
    config = Config.load()
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    env = Environment.detect(cwd)
    profile = config.profile

    provider = OpenAICompatProvider(profile.base_url, profile.api_key)

    registry = default_registry()

    # Session store + optional resume of the most recent conversation.
    from quickcode.core.tasks import TaskBoard
    from quickcode.session.store import SessionStore

    conv_id = None
    if args.continue_session:
        conv_id = SessionStore.most_recent(cwd)
    store = SessionStore(cwd, conv_id)

    # Task board persisted per conversation.
    board_path = cwd / ".quickcode" / "tasks" / store.conv_id / "board.json"
    board = TaskBoard.load(board_path)

    ctx = ToolCtx(
        cwd=cwd,
        read_registry=ReadRegistry(),
        shell_name=env.shell_name,
        platform=env.platform,
        extra={"task_board": board},
    )

    mode_str = args.mode or config.default_mode
    try:
        mode = Mode(mode_str)
    except ValueError:
        mode = Mode.ask

    permissions = PermissionEngine(
        mode=mode,
        rules=Rules.load(cwd),
        root=cwd,
        yolo_accepted=bool(args.yolo),
    )

    # Subagent delegation: give the main agent (depth 0) everything the `agent`
    # tool needs to spawn workers on the catalog's worker model.
    from quickcode.subagents.runner import SubagentDeps

    ctx.extra["subagent"] = SubagentDeps(
        provider=provider,
        profile=profile,
        env=env,
        mode_getter=lambda: permissions.mode,
        cwd=cwd,
        depth=0,
    )

    # The default session model is the catalog's orchestrator (Models tab role),
    # falling back to the profile default when nothing is tagged.
    model = args.model or profile.resolve("orchestrator")
    provider_name = "OpenRouter" if "openrouter.ai" in profile.base_url else profile.base_url

    history = History(
        render_system_prompt(
            env,
            model=model,
            provider=provider_name,
            headless=args.print_mode,
            plan=(mode == Mode.plan),
            orchestration=True,
        )
    )
    if conv_id:
        history.messages = store.load_messages()

    agent = AgentInstance(
        name="main",
        provider=provider,
        registry=registry,
        history=history,
        ctx=ctx,
        permissions=permissions,
        model=model,
        permission_cb=_headless_permission_cb,
        context_length=None,
    )
    if not conv_id:
        store.append_meta(title="", model=model, cwd=str(cwd))
    return agent, config, env, store


async def _run_headless(agent: AgentInstance, prompt: str) -> str:
    return await agent.run_turn(prompt)


def main(argv: list[str] | None = None) -> None:
    raw = list(sys.argv[1:] if argv is None else argv)
    # `quickcode doctor` runs the environment diagnostic and exits.
    if raw and raw[0] == "doctor":
        from quickcode.doctor import main as doctor_main

        raise SystemExit(doctor_main())

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"quickcode {__version__}")
        return

    agent, config, env, store = _build_agent(args)
    profile = config.profile

    no_key_notice = None
    if not profile.api_key:
        no_key_notice = (
            f"No API key set. Set ${profile.api_key_env} or add one in "
            f"Settings (F3 -> Profile) — it is saved encrypted at rest."
        )

    if args.print_mode:
        prompt = args.prompt
        if not prompt:
            prompt = sys.stdin.read()
        if not prompt or not prompt.strip():
            print("error: no prompt given for --print", file=sys.stderr)
            sys.exit(2)
        if no_key_notice:
            print(f"warning: {no_key_notice}", file=sys.stderr)
        result = asyncio.run(_run_headless(agent, prompt))
        print(result)
        return

    from quickcode.app import QuickCodeApp

    app = QuickCodeApp(
        agent,
        config,
        allow_yolo=args.yolo,
        startup_notice=no_key_notice,
        initial_prompt=args.prompt,
        session_store=store,
    )
    app.run()


if __name__ == "__main__":
    main()
