"""The Python encoder and the browser one must agree, character for character.

There are two implementations of TOON in this repository and that is on
purpose: the server encodes what the model reads, and the browser encodes what
the user is shown, from the model's own JSON arguments which never pass through
the server as text. Two implementations of one format drift. This is the test
that notices.

Skipped when node is not installed, because the frontend has no build step and
nothing else in the suite needs a JavaScript runtime -- a missing node must not
turn into a red suite on a machine that only runs the Python side.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from quickcode.context import toon

NODE = shutil.which("node")
TOON_JS = Path(__file__).resolve().parents[1] / "quickcode" / "frontend" / "js" / "toon.js"

pytestmark = pytest.mark.skipif(not NODE, reason="node is not installed")

# One entry per shape the app actually hands either encoder, plus the values
# that decide whether a row can be split back apart.
CASES: list[object] = [
    {"a": 1, "b": "two", "c": None, "d": True},
    {"alerts": ["frost", "wind"]},
    {"matches": [
        {"path": "src/a.py", "line": 1, "text": "def run():"},
        {"path": "src/b.py", "line": 44, "text": "  run()"},
    ]},
    {"forecast": [
        {"day": "Mon", "temp": {"min": -2, "max": 4}, "condition": "snow"},
        {"day": "Tue", "temp": {"min": 1, "max": 7}, "condition": "cloudy"},
    ]},
    {"environments": {
        "production": {"region": "eu-central-1", "replicas": 6, "debug": False},
        "staging": {"region": "eu-central-1", "replicas": 2, "debug": True},
    }},
    {"mixed": [1, {"a": 1, "b": 2}, "x", {}]},
    {"items": [{"a": 1}, {"b": 2}]},
    {"cells": [{"a": [1, 2]}, {"a": [3]}]},
    {"empty": [], "obj": {}, "s": ""},
    {"quoting": [
        {"v": "a,b"},
        {"v": "true"},
        {"v": "42"},
        {"v": "  padded  "},
        {"v": 'say "hi"'},
        {"v": "line\nbreak"},
        {"v": "- dash"},
        {"v": "# hash"},
        {"v": "C:" + chr(92) + "src" + chr(92) + "a.py"},
    ]},
    {"a,b": 1, "plain": 2},
    [{"a": 1}, {"a": 2}],
    "just a string",
    {"deep": {"one": {"two": {"three": {"four": "bottom"}}}}},
]

# Fixtures arrive on stdin rather than in argv: a Windows command line is
# length-capped, and one long grep fixture would silently truncate.
RUNNER = """
import { toon } from %s;
let raw = "";
process.stdin.setEncoding("utf8");
for await (const chunk of process.stdin) raw += chunk;
process.stdout.write(JSON.stringify(JSON.parse(raw).map((c) => toon(c))));
"""


def _js_encode(cases: list[object]) -> list[str]:
    script = RUNNER % json.dumps(TOON_JS.as_uri())
    out = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        input=json.dumps(cases),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert out.returncode == 0, f"node failed: {out.stderr}"
    return json.loads(out.stdout)


def test_both_encoders_produce_the_same_bytes_for_every_shape() -> None:
    js = _js_encode(CASES)
    assert len(js) == len(CASES)
    mismatches = []
    for case, from_js in zip(CASES, js, strict=True):
        from_py = toon.encode(case)
        if from_py != from_js:
            mismatches.append(f"input:  {json.dumps(case)}\npython: {from_py!r}\njs:     {from_js!r}")
    assert not mismatches, "encoders disagree:\n\n" + "\n\n".join(mismatches)
