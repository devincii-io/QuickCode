"""Argv substitution: the rule that makes an authored tool safe by shape.

A command tool's template is a JSON array, one element per argv token, and it
is executed with ``create_subprocess_exec``. No shell is involved at any point,
so a parameter value containing ``; rm -rf /`` or ``$(curl evil)`` is inert
bytes: nothing ever parses it. That is the entire security argument, and it is
structural rather than a filter -- there is no blocklist here to get wrong,
because there is no parser to fool.

The threat model is not the user. It is the *model*, which fills the parameters
and is the one component in this path nobody can audit.

The five rules, exactly:

1. ``{param}`` is replaced **inside** an element. ``"--path={path}"`` yields one
   element whatever the value contains. Values are never re-split on
   whitespace, never re-quoted, never re-parsed.
2. An element that is **exactly** ``{param}``, where the parameter is
   ``list``-typed, expands to one element per item. This is the only expansion
   and it is explicit at both ends.
3. An element that is exactly ``{param}`` whose value is absent or empty is
   **dropped**. An element mixing literal text with an empty placeholder is
   kept with an empty substitution -- if you want a flag to disappear, give it
   its own element.
4. A ``bool`` parameter may only appear as a whole element. True emits its
   ``flag`` (defaulting to ``--<name>``); false drops the element.
5. ``{{`` and ``}}`` are literal braces. An unknown ``{name}`` is a validation
   error, never a silent empty string -- a typo that quietly dropped an
   argument would produce a command that runs and does the wrong thing, which
   is worse than one that refuses to load.

This module is pure: no filesystem, no runtime imports. It is shared by the
validator (which checks a template it will never run) and by ``CommandTool``
(which runs one it has already checked), so the two cannot disagree about what
a template means.
"""

from __future__ import annotations

import re
from typing import Any

# ``{{`` / ``}}`` first so an escaped brace is never read as a placeholder.
_TOKEN = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}")


def placeholders(element: str) -> list[str]:
    """Every parameter name referenced in one argv element, in order."""
    out: list[str] = []
    for match in _TOKEN.finditer(element):
        name = match.group(1)
        if name is not None:
            out.append(name)
    return out


def whole_placeholder(element: str) -> str:
    """The name when the element is *nothing but* one placeholder, else ""."""
    names = placeholders(element)
    if len(names) == 1 and element == "{" + names[0] + "}":
        return names[0]
    return ""


def scalar(value: Any) -> str:
    """One value as one argv token. Never splits, never quotes."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def render_element(element: str, values: dict[str, Any]) -> str:
    """In-element substitution with ``{{``/``}}`` unescaping."""
    out: list[str] = []
    cursor = 0
    for match in _TOKEN.finditer(element):
        out.append(element[cursor:match.start()])
        name = match.group(1)
        if name is None:
            out.append("{" if match.group(0) == "{{" else "}")
        else:
            out.append(scalar(values.get(name)))
        cursor = match.end()
    out.append(element[cursor:])
    return "".join(out)


def render_argv(
    template: list[str] | tuple[str, ...],
    params: dict[str, Any],
    values: dict[str, Any],
) -> list[str]:
    """The resolved argv. ``params`` maps name -> a param carrying ``.type``.

    Every model-supplied value lands as a whole element (or as a substring of
    exactly one element). Nothing here can turn one value into two tokens
    except rule 2, which requires the author to have declared a list parameter
    *and* to have given it an element of its own.
    """
    out: list[str] = []
    for element in template:
        name = whole_placeholder(element)
        if name and name in params:
            kind = getattr(params[name], "type", "string")
            value = values.get(name)
            if kind == "list":
                items = value if isinstance(value, (list, tuple)) else []
                out.extend(scalar(item) for item in items if scalar(item) != "")
                continue
            if kind == "bool":
                if bool(value):
                    flag = getattr(params[name], "flag", "") or f"--{name.replace('_', '-')}"
                    out.append(flag)
                continue
            text = scalar(value)
            if text == "":
                continue  # rule 3: a whole-element placeholder with nothing in it
            out.append(text)
            continue
        out.append(render_element(element, values))
    return out
