"""Command hooks: the user's own scripts, run at the agent loop's seams.

``config``    what the settings files declare, and the trust gate over it
``protocol``  what an exit code and a line of JSON mean
``runner``    one command, a payload on stdin, a deadline
``events``    the ``hook_run`` record the session log keeps
``plugin``    ``CommandHooks``, the ``LoopHook`` that ties them to the loop
``specs``     the Settings cards

docs/HOOKS.md is the user-facing description.
"""

from quickcode.hooks.config import EVENTS, HookCommand, HookConfig, load_hooks
from quickcode.hooks.plugin import CommandHooks, child_hooks, session_hooks

__all__ = [
    "EVENTS",
    "CommandHooks",
    "HookCommand",
    "HookConfig",
    "child_hooks",
    "load_hooks",
    "session_hooks",
]
