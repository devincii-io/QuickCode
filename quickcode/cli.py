"""QuickCode CLI entry point.

Parses arguments, assembles the agent (provider, registry, permissions,
history), and either runs one headless turn (``-p/--print``) or launches the
local web app (FastAPI on 127.0.0.1 in a native app window).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from quickcode.config import Config, Environment
from quickcode.core.agent import AgentInstance, PermissionOutcome, PermissionRequest
from quickcode.core.history import History
from quickcode.core.permissions import Mode, PermissionEngine, Rules
from quickcode.kernel.resolve import runtime_limits
from quickcode.kernel.state import prompt_overrides
from quickcode.prompts.system import render_system_prompt
from quickcode.tools.base import ReadRegistry, ToolCtx
from quickcode.tools.registry import default_registry

if TYPE_CHECKING:
    from quickcode.session.recorder import TranscriptRecorder

try:
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("quickcode")
except Exception:  # not installed as a package (running from source)
    __version__ = "0.1.0-dev"


def _say(message: str) -> None:
    """Print a status line, unless there is nowhere to print it.

    ``quickcode-app`` runs under pythonw, where a GUI process has no console
    and ``sys.stdout`` is ``None``.
    """
    if sys.stdout is None:
        return
    print(message)


def _bind_null_streams() -> None:
    """Point ``sys.stdout``/``sys.stderr`` at the null device when they are ``None``.

    Under pythonw both are ``None``. Guarding our own prints is not enough:
    uvicorn installs logging handlers around ``sys.stdout``/``sys.stderr``, and
    any third-party writer would hit the same hole. Giving them a real file
    keeps every writer happy for the life of the process.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


async def _headless_permission_cb(request: PermissionRequest) -> PermissionOutcome:
    return PermissionOutcome(allow=False, deny_message="headless: not permitted")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quickcode", description="QuickCode coding agent")
    # `qc [path] [prompt]`: the first positional is the project directory when
    # it names one (`qc .`), otherwise it is the prompt (the original shape).
    parser.add_argument("first", nargs="?", default=None,
                         help="project directory, or the initial prompt")
    parser.add_argument("second", nargs="?", default=None,
                         help="initial prompt, when a directory was given first")
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
                         help="continue the most recent session")
    parser.add_argument("--port", type=int, default=None,
                         help="local web port (default: 8642, or a free port)")
    parser.add_argument("--no-browser", action="store_true",
                         help="don't open any window (prints the URL)")
    parser.add_argument("--browser", action="store_true",
                         help="open in the default browser instead of the app window")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    return parser


def _resolve_positionals(args: argparse.Namespace) -> None:
    """Fold ``[path] [prompt]`` into ``args.cwd`` / ``args.prompt``.

    ``qc .`` and ``qc C:\\proj`` open a project; ``qc "fix the build"`` keeps
    the original prompt-only shape. An explicit ``--cwd`` always wins.
    """
    first, second = args.first, args.second
    path: str | None = None
    prompt: str | None = first
    if first is not None and _looks_like_dir(first):
        path = first
        prompt = second
    elif second is not None:
        print(
            "error: with two positional arguments the first must be a project directory",
            file=sys.stderr,
        )
        raise SystemExit(2)
    args.prompt = prompt
    args.project_given = bool(path or args.cwd)
    if args.cwd is None:
        args.cwd = path


def _looks_like_dir(value: str) -> bool:
    try:
        return Path(value).expanduser().is_dir()
    except OSError:
        return False


def _build_agent(args: argparse.Namespace):
    config = Config.load()
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    env = Environment.detect(cwd)
    profile = config.profile

    # Imported here rather than at module scope: this module is what `qc` and
    # the windowed entry point both load first, and a top-level import would
    # drag the OpenAI SDK in before anything is on screen.
    from quickcode.providers.openai_compat import OpenAICompatProvider

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

    # The active permission profile, exactly as the server applies it: its rules
    # merge with the project's own, and its mode is where the run starts unless
    # `--mode` named one outright. `-c` makes no difference -- no per-session
    # mode is stored for a resume to keep, so honouring the profile only there
    # would mean a continued run came back wider than the one it continues.
    # `manager.open()` draws the same two lines, at more length.
    from quickcode.core.profiles import effective as effective_posture

    posture_mode, rules, _posture = effective_posture(cwd, Rules.load(cwd), fallback=mode)
    if not args.mode:
        mode = posture_mode

    permissions = PermissionEngine(
        mode=mode,
        rules=rules,
        root=cwd,
        yolo_accepted=bool(args.yolo),
        specs=registry.permission_specs(),
    )

    # Subagent delegation: give the main agent (depth 0) everything the `agent`
    # tool needs to spawn workers on the catalog's worker model.
    from quickcode.subagents.runner import SubagentDeps

    # Resolved once here, like the server does at session open, so the CLI
    # obeys the same declared limits instead of a second set of constants.
    limits = runtime_limits(cwd)

    # The trace: the same recorder the web path runs on, so a `-p` session log
    # is the same artefact a UI session leaves behind rather than a second,
    # thinner shape of one.
    from quickcode.session.recorder import TranscriptRecorder

    recorder = TranscriptRecorder(store)

    ctx.extra["subagent"] = SubagentDeps(
        provider=provider,
        profile=profile,
        env=env,
        mode_getter=lambda: permissions.mode,
        cwd=cwd,
        depth=0,
        # Subagent activity belongs in the log for the same reason it does in
        # the UI: without it the trace shows a tool call and no worker. Both
        # brackets, or the trace shows a worker that starts and never stops --
        # and every ``-p`` delegation is blocking, since nothing here outlives
        # the turn to own a detached one.
        on_pane=recorder.on_subagent,
        on_done=recorder.on_subagent_done,
        tool_pool=list(registry.tools.values()),
        limits=limits,
    )

    # Model precedence: explicit --model, then the last model picked via F2
    # (persisted), then the catalog's orchestrator role, then the profile default.
    model = args.model or config.last_model or profile.resolve("orchestrator")
    provider_name = "OpenRouter" if "openrouter.ai" in profile.base_url else profile.base_url

    history = History(
        render_system_prompt(
            env,
            model=model,
            provider=provider_name,
            headless=args.print_mode,
            plan=(mode == Mode.plan),
            orchestration=True,
            overrides=prompt_overrides(cwd),
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
        limits=limits,
    )
    if not conv_id:
        store.append_meta(title="", model=model, cwd=str(cwd))
    # What the model already carries, so a resumed session re-persists nothing.
    recorder.persisted = len(history.messages)
    return agent, config, env, store, recorder


async def _warm_context_length(agent: AgentInstance) -> None:
    """Learn the model's context window while the turn is already running.

    The end-of-turn compaction check is a no-op without it (``context_pct()``
    returns ``None``), but asking the provider for its catalog up front would
    put a network round trip in front of every ``-p`` invocation. So it is
    fetched alongside the turn, and a failure leaves things exactly as they
    were: no meter, no compaction.
    """
    if agent.context_length is not None or not agent.limits.compaction_enabled:
        return
    try:
        for info in await agent.provider.list_models():
            if info.id == agent.model:
                agent.context_length = info.context_length
                return
    except Exception:
        return


async def _run_headless(
    agent: AgentInstance, recorder: TranscriptRecorder, prompt: str
) -> str:
    """One traceable headless turn: the log a UI turn would have left, then
    the final response for stdout."""
    # The trace has to show everything the model sees, and a resumed run may
    # have re-rendered the prompt — same reason the server logs it at open.
    recorder.emit({"type": "system_prompt", "text": agent.history.system_prompt})
    warm = asyncio.create_task(_warm_context_length(agent))
    try:
        return await recorder.record_turn(agent, prompt)
    finally:
        warm.cancel()


def main(argv: list[str] | None = None) -> None:
    raw = list(sys.argv[1:] if argv is None else argv)
    # `quickcode doctor` runs the environment diagnostic and exits.
    if raw and raw[0] == "doctor":
        from quickcode.doctor import main as doctor_main

        raise SystemExit(doctor_main())

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.version:
        _say(f"quickcode {__version__}")
        return

    _resolve_positionals(args)

    if args.print_mode:
        agent, config, env, store, recorder = _build_agent(args)
        prompt = args.prompt
        if not prompt:
            prompt = sys.stdin.read()
        if not prompt or not prompt.strip():
            print("error: no prompt given for --print", file=sys.stderr)
            sys.exit(2)
        if not config.profile.api_key:
            print(
                f"warning: no API key set. Set ${config.profile.api_key_env} "
                "or add one in Settings.",
                file=sys.stderr,
            )
        result = asyncio.run(_run_headless(agent, recorder, prompt))
        print(result)
        return

    from quickcode.session.store import SessionStore
    from quickcode.webapp import run_webapp

    config = Config.load()
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    env = Environment.detect(cwd)
    if args.model:
        config.last_model = args.model
    resume = SessionStore.most_recent(cwd) if args.continue_session else None
    run_webapp(
        cwd=cwd,
        config=config,
        env=env,
        allow_yolo=args.yolo,
        default_mode=args.mode,
        port=args.port,
        open_browser=not args.no_browser,
        native=not args.browser,
        initial_resume=resume,
    )


def main_app() -> None:
    """GUI entry point for the Start Menu / Desktop shortcut and the frozen
    ``QuickCodeApp.exe``.

    Equivalent to ``quickcode --cwd <home>``: the user's home directory is the
    *default* project, and the app window opens on it. Launched without a
    console (pythonw, or a windowed PyInstaller build), so the console streams
    are patched up before anything writes to them.

    The home directory is only a fallback. A launch that already names a
    project keeps it -- which is what makes the Explorer context menu work:
    it runs ``QuickCodeApp.exe "%V"`` on the clicked folder, and prepending
    ``--cwd <home>`` unconditionally would silently discard it (argparse takes
    the *last* ``--cwd``, and the folder arrives as a positional).
    """
    _bind_null_streams()
    args = list(sys.argv[1:])
    named_project = any(a.startswith("--cwd") for a in args) or any(
        _looks_like_dir(a) for a in args if not a.startswith("-")
    )
    if not named_project:
        args = ["--cwd", str(Path.home()), *args]
    main(args)


if __name__ == "__main__":
    main()
