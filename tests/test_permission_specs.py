"""Every built-in tool's permission shape, as audited.

The engine gates a tool by what the tool declares, so a declaration is a
security claim: a tool that writes and declares ``mutates=False`` is waved
through in every mode. This table is the audit, kept as a test -- changing a
tool's shape means changing a line here, in a diff somebody reads.

Audit notes for the non-obvious rows:

- ``agent`` / ``send_message`` start or resume a child, whose own calls are
  gated by its capped mode and by the session's deny and ask rules, and whose
  prompts are auto-denied -- so the spawn itself needs no prompt.
- ``task_create`` / ``task_update`` write QuickCode's own task board, keyed
  by an id the board assigns; nothing the model passes names a file.
- ``plan`` is answered by the plan-review hook before the gate is reached.
- ``web_fetch`` and ``web_search`` do not write, and are still mutating: a
  request is a way out for anything the agent has read.
"""

from __future__ import annotations

from quickcode.core.permissions import DEFAULT_SPEC, PermissionSpec
from quickcode.tools.registry import default_registry

# name: (mutates, target_field, path_target, shell)
AUDITED = {
    "read": (False, "file_path", True, False),
    "grep": (False, "path", True, False),
    "glob": (False, "path", True, False),
    "write": (True, "file_path", True, False),
    "edit": (True, "file_path", True, False),
    "bash": (True, "command", False, True),
    "web_fetch": (True, "url", False, False),
    "web_search": (True, "query", False, False),
    "agent": (False, "agent_type", False, False),
    "agent_status": (False, "agent_id", False, False),
    "agent_result": (False, "agent_id", False, False),
    "send_message": (False, "agent_id", False, False),
    "plan": (False, None, False, False),
    "task_create": (False, None, False, False),
    "task_update": (False, "task_id", False, False),
    "task_list": (False, None, False, False),
    "task_get": (False, "task_id", False, False),
}


def _shape(spec: PermissionSpec) -> tuple:
    return (spec.mutates, spec.target_field, spec.path_target, spec.shell)


def test_every_builtin_tool_declares_its_own_permission():
    """Nothing leans on the inherited default, which is safe for a plugin that
    forgot and wrong for a read-only tool (it would prompt for every read)."""
    for name, tool in default_registry().tools.items():
        declared = "permission" in type(tool).__dict__ or "permission" in vars(tool)
        assert declared, f"{name} inherits DEFAULT_SPEC"
        assert tool.permission is not DEFAULT_SPEC, name


def test_every_builtin_tool_has_its_audited_shape():
    tools = default_registry().tools
    assert set(tools) == set(AUDITED), "a tool was added or removed; audit it here"
    for name, tool in tools.items():
        assert _shape(tool.permission) == AUDITED[name], name


def test_every_tool_that_takes_a_path_is_gated_as_a_path():
    """The shape of the 2.4.1 finding: grep and glob named a path and did not
    say so, and read anywhere on the machine unprompted."""
    path_fields = {"path", "file_path", "dir", "directory", "file", "cwd", "root"}
    for name, tool in default_registry().tools.items():
        fields = set(getattr(tool.Input, "model_fields", {}))
        if fields & path_fields:
            assert tool.permission.path_target, f"{name} takes a path but does not gate it"
