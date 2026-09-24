"""QuickCode CLI entry point.

Parses arguments and either runs one headless turn (``-p/--print``) on the
session ``session/assemble.py`` builds -- the one the app would open -- or
launches the local web app (FastAPI on 127.0.0.1 in a native app window).
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
from quickcode.core.permissions import Mode

if TYPE_CHECKING:
    from quickcode.session.recorder import TranscriptRecorder

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


def _port(value: str) -> int:
    """A TCP port a server can listen on, or an argument error now rather
    than an OverflowError from the socket layer once the window is open."""
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a port number: {value!r}") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"port must be between 1 and 65535, not {port}")
    return port


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
    parser.add_argument("--port", type=_port, default=None,
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
    cwd = _project_dir(args)
    env = Environment.detect(cwd)
    profile = config.profile
    armed = bool(args.yolo or config.allow_yolo)
    _refuse_unarmed_yolo(args.mode, armed=armed)

    # Imported here rather than at module scope: this module is what `qc` and
    # the windowed entry point both load first, and a top-level import would
    # drag the provider SDKs in before anything is on screen. The same factory
    # the app uses, so `-p` talks to the backend the profile names.
    from quickcode.kernel.resolve import session_pool
    from quickcode.plugins import loader
    from quickcode.session import assemble
    from quickcode.session.recorder import TranscriptRecorder
    from quickcode.session.store import SessionStore
    from quickcode.tools.registry import default_registry

    provider = loader.make_provider(profile.provider, profile.base_url, profile.api_key)

    conv_id = None
    if args.continue_session:
        conv_id = SessionStore.most_recent(cwd)
        if conv_id is None:
            _note(f"no earlier session in {cwd}; starting a new one")

    # The session the app would open on this project -- its pool, its
    # composition, its posture, its prompt -- assembled by the same code.
    session = assemble.build_session(
        cwd, config, env, provider,
        pool=session_pool(cwd, default_registry().tools.values()),
        conv_id=conv_id,
        mode=args.mode,
        model=args.model,
        headless=True,
        yolo_armed=armed,
        permission_cb=_headless_permission_cb,
    )
    agent = session.agent
    if session.unarmed_yolo:
        _note("your settings, composition or permission profile ask for yolo mode, "
              "which is not enabled (pass --yolo, or turn it on in Settings); "
              f"running in {agent.permissions.mode.value} mode")

    # The trace: the same recorder the web path runs on, so a `-p` session log
    # is the same artefact a UI session leaves behind rather than a second,
    # thinner shape of one.
    recorder = TranscriptRecorder(session.store)
    session.wire(
        # Subagent activity belongs in the log for the same reason it does in
        # the UI: without it the trace shows a tool call and no worker. Both
        # brackets, or the trace shows a worker that starts and never stops --
        # and every ``-p`` delegation is blocking, since nothing here outlives
        # the turn to own a detached one.
        on_pane=recorder.on_subagent,
        on_done=recorder.on_subagent_done,
        # Background shell jobs work within the one turn a `-p` run has --
        # start a server, test against it, stop it -- and `_run_headless`
        # kills whatever is left when that turn ends, because the process is
        # about to.
        on_bash_event=recorder.emit,
    )
    session.begin_log()
    # What the model already carries, so a resumed session re-persists nothing.
    recorder.persisted = len(agent.history.messages)
    return agent, config, env, session.store, recorder


async def _warm_context_length(agent: AgentInstance) -> None:
    """Learn the model's context window while the turn is already running.

    The end-of-turn compaction check is a no-op without it (``context_pct()``
    returns ``None``), but asking the provider for its catalog up front would
    put a network round trip in front of every ``-p`` invocation. So it is
    fetched alongside the turn, and a failure leaves things exactly as they
    were: no meter, no compaction.

    The subagents it spawns run a context guard of their own, on models of
    their own, so they are handed the same catalog's windows.
    """
    if agent.context_length is not None or not agent.limits.compaction_enabled:
        return
    try:
        catalog = await agent.provider.list_models()
    except Exception:
        return
    windows = {m.id: m.context_length for m in catalog if m.context_length}
    # Still None unless the context guard learned the window from a refusal
    # while this was in flight -- the provider's own word beats the catalog's.
    if agent.context_length is None:
        agent.context_length = windows.get(agent.model)
    ctx = getattr(agent, "ctx", None)
    deps = ctx.extra.get("subagent") if ctx is not None else None
    if deps is not None and deps.context_window is None:
        deps.context_window = windows.get


async def _run_headless(
    agent: AgentInstance, recorder: TranscriptRecorder, prompt: str
) -> tuple[str, str | None]:
    """One traceable headless turn: the log a UI turn would have left, then
    the final response for stdout and the error that ended it, if any."""
    from quickcode import headless

    # The trace has to show everything the model sees, and a resumed run may
    # have re-rendered the prompt — same reason the server logs it at open.
    recorder.emit({"type": "system_prompt", "text": agent.history.system_prompt})
    watch = headless.watch_for_failure(agent.bus)
    warm = asyncio.create_task(_warm_context_length(agent))
    try:
        text = await recorder.record_turn(agent, prompt)
    finally:
        warm.cancel()
        bash_jobs = agent.ctx.extra.get("bash_jobs") if agent.ctx else None
        if bash_jobs is not None:
            await asyncio.to_thread(bash_jobs.close)
    return text, headless.turn_failure(watch)


def _note(message: str) -> None:
    from quickcode import headless

    headless.emit(f"note: {message}", sys.stderr)


def _project_dir(args: argparse.Namespace) -> Path:
    """The project directory the run is about, which must already exist.

    A typo used to be taken at its word: the session store created the path,
    a ``.quickcode`` inside it and a session file, and the agent went to work
    in an empty directory.
    """
    if not args.cwd:
        return Path.cwd()
    path = Path(args.cwd).expanduser()
    if not path.is_dir():
        problem = "not a directory" if path.exists() else "no such directory"
        print(f"error: {problem}: {args.cwd}", file=sys.stderr)
        raise SystemExit(2)
    return path.resolve()


def _refuse_unarmed_yolo(mode: str | None, *, armed: bool) -> None:
    """``--mode yolo`` without ``--yolo`` (or Settings' allow_yolo) is an
    argument error, raised before anything is built.

    The rule the server applies (``manager.set_mode``/``apply_posture``): yolo
    needs arming, whoever asks for it. Asked for by settings, a composition or
    a profile instead, the run starts in ask and says so -- the same fallback
    ``session/assemble.py`` gives the app.
    """
    if mode != Mode.yolo.value or armed:
        return
    print("error: --mode yolo runs every tool without asking; it needs --yolo "
          "(or allow_yolo in Settings)", file=sys.stderr)
    raise SystemExit(2)


def _main_headless(args: argparse.Namespace) -> int:
    from quickcode import headless

    _project_dir(args)
    prompt = args.prompt or headless.prompt_from_stdin(sys.stdin)
    if not prompt or not prompt.strip():
        print("error: no prompt given for --print (pass one, or pipe it in)", file=sys.stderr)
        return headless.EXIT_USAGE
    agent, config, env, store, recorder = _build_agent(args)
    if not config.profile.api_key:
        print(
            f"warning: no API key set. Set ${config.profile.api_key_env} "
            "or add one in Settings.",
            file=sys.stderr,
        )
    try:
        result, failure = asyncio.run(_run_headless(agent, recorder, prompt))
    except KeyboardInterrupt:
        # The recorder has already closed the log out; a traceback here would
        # only bury the one line that matters.
        headless.emit("interrupted", sys.stderr)
        return headless.EXIT_INTERRUPTED
    if result or not failure:
        headless.emit(result)
    if failure:
        headless.emit(f"error: {failure}", sys.stderr)
        return headless.EXIT_TURN_FAILED
    return headless.EXIT_OK


def main(argv: list[str] | None = None) -> None:
    raw = list(sys.argv[1:] if argv is None else argv)
    # `quickcode doctor` runs the environment diagnostic and exits.
    if raw and raw[0] == "doctor":
        from quickcode.doctor import main as doctor_main

        raise SystemExit(doctor_main())
    # `qc why "<command>"` / `quickcode permissions explain`: the permission dry run.
    if raw and raw[0] in ("why", "permissions"):
        from quickcode.permission_cli import main as permissions_main

        raise SystemExit(permissions_main(raw))

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.version:
        from quickcode import __version__

        _say(f"quickcode {__version__}")
        return

    _resolve_positionals(args)

    if args.print_mode:
        code = _main_headless(args)
        if code:
            raise SystemExit(code)
        return

    from quickcode.session.store import SessionStore
    from quickcode.webapp import run_webapp

    config = Config.load()
    cwd = _project_dir(args)
    env = Environment.detect(cwd)
    if args.model:
        config.last_model = args.model
    resume = SessionStore.most_recent(cwd) if args.continue_session else None
    if args.continue_session and resume is None:
        _note(f"no earlier session in {cwd}; starting a new one")
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
