"""What more than one family of plugin cards shares."""

from __future__ import annotations

import json
from typing import Any

from quickcode.kernel.facts import tool_signature
from quickcode.kernel.spec import PluginView, Recourse


def lazy_view(fmt: str, content: str, title: str = "", path: str = ""):
    return lambda: PluginView(format=fmt, content=content, title=title, path=path)


def schema_text(tool: Any) -> tuple[str, str]:
    """``(payload, signature)``: the tool's schema as JSON, and its one-liner.

    A tool whose schema cannot be built still gets a card; the payload then
    says why and the signature is empty.
    """
    try:
        schema = tool.schema()
        payload = json.dumps(
            {"name": schema.name, "description": schema.description,
             "parameters": schema.parameters},
            indent=2,
        )
        return payload, tool_signature(schema.name, schema.parameters)
    except Exception as exc:
        return f"schema unavailable: {exc}", ""


READ_ONLY_LOCKED_BECAUSE = (
    "Parallelism and the default permission answer are both read off this flag "
    "on every call. A tool that mutated while claiming to be read-only would "
    "run concurrently with a write and skip the prompt, so the tool declares it "
    "in code and nothing outside the tool can override it."
)

READ_ONLY_RECOURSE = Recourse(
    "settings", "Gate this tool with a permission rule instead", "runtime.permissions"
)
