"""TOON: the encoding the model reads structured data in.

Token-Oriented Object Notation encodes the JSON data model, but declares a
uniform array's field list once in a header instead of repeating it on every
element::

    matches[3]{path,line,text}:
      src/a.py,12,def run():
      src/b.py,44,  run()
      src/c.py,91,"# run() is the entry point"

Two things are being bought here, and only one of them is tokens:

  - The header carries a row count. A model handed ``[200]`` that reads 198
    rows knows the answer was cut; with a bare list of lines it cannot tell
    truncation from an empty tail.
  - Fields are separated by a delimiter that values are *quoted against*. The
    old ``path:line:text`` rendering went ambiguous the moment a Windows path
    carried a drive letter, and nothing downstream could recover the split.

This is an encoder only. Nothing in QuickCode parses TOON back, because
nothing needs to: JSON stays the program's copy of the data and this is the
view handed to the model. A decoder we never call would be a second
implementation of the format to keep correct, for no reader.

Scope: model-facing text only. HTTP responses, session records and config
files stay JSON, because other programs read those.
"""

from __future__ import annotations

import math
import re
from typing import Any

# The spec allows comma, tab or pipe. Comma reads best and tokenizes well; tab
# is a little cheaper and is there for callers who know their values never
# contain one.
COMMA = ","
TAB = "\t"
PIPE = "|"

INDENT = 2

# Depth this far down is a cycle or a bug, and either way the model cannot use
# the result. Bail with a marker rather than blowing the stack.
MAX_DEPTH = 24

# Bare strings that would read back as something other than a string.
_LITERALS = frozenset({"true", "false", "null"})
_NUMBERISH = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}

# Characters that end a key or would be read as structure inside a header.
_KEY_BREAKERS = ':"{}[],'


def encode(value: Any, *, delimiter: str = COMMA, indent: int = INDENT) -> str:
    """Render ``value`` as TOON. Deterministic: same input, same bytes."""
    lines: list[str] = []
    _write(None, value, lines, 0, delimiter, indent, 0)
    return "\n".join(lines)


def fenced(value: Any, *, delimiter: str = COMMA) -> str:
    """``encode`` inside a ```toon fence.

    Worth the handful of extra tokens exactly once per result: naming the
    format is what stops a model reading a header line as prose.
    """
    return "```toon\n" + encode(value, delimiter=delimiter) + "\n```"


# ---- the four forms ----------------------------------------------------


def _write(
    key: str | None,
    value: Any,
    lines: list[str],
    depth: int,
    d: str,
    indent: int,
    guard: int,
) -> None:
    pad = " " * (depth * indent)
    label = "" if key is None else _fmt_key(key, d)

    if guard > MAX_DEPTH:
        lines.append(f"{pad}{label}: <too deeply nested>")
        return

    if isinstance(value, dict):
        _write_object(label, value, lines, pad, depth, d, indent, guard)
    elif isinstance(value, (list, tuple)):
        _write_array(label, list(value), lines, pad, depth, d, indent, guard)
    else:
        sep = ": " if label else ""
        lines.append(f"{pad}{label}{sep}{_fmt_scalar(value, d)}")


def _write_object(
    label: str,
    obj: dict,
    lines: list[str],
    pad: str,
    depth: int,
    d: str,
    indent: int,
    guard: int,
) -> None:
    if not obj:
        # Keyless (a list element) an empty object is a bare dash, so leave the
        # line empty here and let the caller put the dash on it.
        lines.append(f"{pad}{label}:" if label else pad)
        return

    # Keyed tabular: an object whose values are all uniform objects. Config
    # maps and records-by-id land here, and each row keeps its own key.
    spec = _table_spec(list(obj.values())) if len(obj) > 1 else None
    if spec is not None:
        lines.append(f"{pad}{label}[{len(obj)}:]{{{_header(spec, d)}}}:")
        inner = " " * ((depth + 1) * indent)
        for k, row in obj.items():
            lines.append(f"{inner}{_fmt_key(str(k), d)}: {d.join(_cells(row, spec, d))}")
        return

    if label:
        lines.append(f"{pad}{label}:")
        depth += 1
    for k, v in obj.items():
        _write(str(k), v, lines, depth, d, indent, guard + 1)


def _write_array(
    label: str,
    items: list,
    lines: list[str],
    pad: str,
    depth: int,
    d: str,
    indent: int,
    guard: int,
) -> None:
    n = len(items)
    if not items:
        lines.append(f"{pad}{label}[0]:")
        return

    # Inline form: primitives live on the header line.
    if all(_is_scalar(x) for x in items):
        lines.append(f"{pad}{label}[{n}]: " + d.join(_fmt_scalar(x, d) for x in items))
        return

    # Tabular form: the field list declared once, then one row per element.
    spec = _table_spec(items)
    if spec is not None:
        lines.append(f"{pad}{label}[{n}]{{{_header(spec, d)}}}:")
        inner = " " * ((depth + 1) * indent)
        for row in items:
            lines.append(inner + d.join(_cells(row, spec, d)))
        return

    # List form: the fallback that can hold anything.
    lines.append(f"{pad}{label}[{n}]:")
    lead = (depth + 1) * indent
    for item in items:
        # Rendered one level deeper than the dash, so an element's second and
        # later lines already sit under its first one.
        block: list[str] = []
        _write(None, item, block, depth + 2, d, indent, guard + 1)
        if not block:
            block = [" " * (lead + indent)]
        first = block[0]
        block[0] = (first[:lead] + "- " + first[lead + indent :]).rstrip()
        lines.extend(block)


# ---- deciding whether a table is possible ------------------------------

# A spec is a list of (field name, sub-spec or None). A sub-spec means the
# field holds uniform nested objects, which fold into the header as
# ``name{a,b}`` while the rows stay flat.
Spec = list[tuple[str, Any]]


def _table_spec(rows: list) -> Spec | None:
    """The field list for a tabular rendering, or None if the rows disagree."""
    if not rows or not all(isinstance(r, dict) and r for r in rows):
        return None
    names = [str(k) for k in rows[0]]
    wanted = set(names)
    if any({str(k) for k in r} != wanted for r in rows):
        return None
    if len(wanted) != len(names):  # two keys that stringify the same
        return None

    spec: Spec = []
    for name in names:
        column = [row[_key_named(row, name)] for row in rows]
        if all(_is_scalar(v) for v in column):
            spec.append((name, None))
        elif all(isinstance(v, dict) and v for v in column):
            sub = _table_spec(column)
            if sub is None:
                return None
            spec.append((name, sub))
        else:
            # A list in a cell has nowhere to go on a flat row.
            return None
    return spec


def _key_named(row: dict, name: str) -> Any:
    """The real key of ``row`` whose ``str()`` is ``name``."""
    if name in row:
        return name
    for k in row:
        if str(k) == name:
            return k
    raise KeyError(name)


def _header(spec: Spec, d: str) -> str:
    parts = []
    for name, sub in spec:
        key = _fmt_key(name, d)
        parts.append(key if sub is None else f"{key}{{{_header(sub, d)}}}")
    return d.join(parts)


def _cells(row: dict, spec: Spec, d: str) -> list[str]:
    out: list[str] = []
    for name, sub in spec:
        value = row[_key_named(row, name)]
        if sub is None:
            out.append(_fmt_scalar(value, d))
        else:
            out.extend(_cells(value, sub, d))
    return out


# ---- scalars -----------------------------------------------------------


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, bool, int, float))


def _fmt_scalar(value: Any, d: str) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # JSON has no spelling for these, so neither do we; saying so out loud
        # beats a bare `null` the model would read as "no value".
        if math.isnan(value) or math.isinf(value):
            return _quote(repr(value))
        return repr(value)
    if isinstance(value, str):
        return _quote(value) if _needs_quote(value, d) else value
    return _quote(str(value))


def _needs_quote(s: str, d: str) -> bool:
    if s == "" or s != s.strip():
        return True
    if d in s or '"' in s or _CONTROL.search(s):
        return True
    if s in _LITERALS or _NUMBERISH.match(s):
        return True
    # Leading punctuation that would read as structure rather than content.
    return s[0] in "#-[{"


def _quote(s: str) -> str:
    out = ['"']
    for ch in s:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _fmt_key(key: str, d: str) -> str:
    s = str(key)
    if not s or s != s.strip():
        return _quote(s)
    if d in s or any(c in s for c in _KEY_BREAKERS) or _CONTROL.search(s):
        return _quote(s)
    return s
