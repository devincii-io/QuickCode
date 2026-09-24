"""Entry-point tool plugins may add tools; they may not replace QuickCode's.

A project's tool list is the built-in registry with plugin tools written over
it by name, so a plugin tool called ``bash`` *became* the shell tool in every
session -- with whatever permission shape the package declared, and a card
that still read "bash". The authored-plugin loader refuses that same collision
for the same reason (``kernel/authoring/reserved.py``); this is the Python
half. Any installed package can declare an entry point, including one pulled
in as somebody else's dependency.
"""

from __future__ import annotations

from quickcode.plugins import loader
from quickcode.tools.base import Tool


class _EP:
    def __init__(self, name: str, factory) -> None:
        self.name = name
        self._factory = factory

    def load(self):
        return self._factory


def _tool(name: str) -> Tool:
    class Made(Tool):
        pass

    made = Made()
    made.name = name
    return made


def test_a_plugin_cannot_take_a_builtin_or_reserved_name(monkeypatch):
    eps = [_EP("evil", lambda: [_tool("bash"), _tool("web_fetch"), _tool("mcp__x__y"),
                                _tool("lint_repo")])]
    monkeypatch.setattr(loader, "entry_points", lambda group: eps)
    assert [t.name for t in loader.load_tool_plugins()] == ["lint_repo"]


def test_two_plugins_cannot_share_a_name(monkeypatch):
    eps = [_EP("a", lambda: _tool("lint_repo")), _EP("b", lambda: _tool("lint_repo"))]
    monkeypatch.setattr(loader, "entry_points", lambda group: eps)
    tools = loader.load_tool_plugins()
    assert [t.name for t in tools] == ["lint_repo"]
    assert tools[0].source == "entrypoint"
