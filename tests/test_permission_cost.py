"""The gate must answer in bounded time, whatever the command line looks like.

A model can be talked into writing any command, and the permission check runs
before anything else on every one of them. Three inputs were pathological while
the shell reading was being hardened: a long unbroken word (the fork-bomb regex
started a function name at every character -- 90 s for 100 KB), nested
substitutions (every level re-evaluated every deeper one), and a long heredoc
(every word resolved on disk, the project root re-resolved three times a word).
The bounds below are generous on purpose; each case used to miss by orders of
magnitude.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from quickcode.core.permissions import Decision, Mode, PermissionEngine, Rules


def _timed(command: str, root: Path) -> tuple[Decision, float]:
    engine = PermissionEngine(mode=Mode.ask, rules=Rules(), root=root)
    engine.evaluate("bash", "true")  # the tool registry import, once
    start = time.perf_counter()
    decision = engine.evaluate("bash", command)
    return decision, time.perf_counter() - start


@pytest.mark.parametrize("command", [
    "echo " + "a" * 100_000,
    "x" * 100_000,
    "f" * 20_000 + "() {",
    "echo " + "$(" * 300 + "x" + ")" * 300,
    "echo " + "{" * 20_000,
    "echo " + "(" * 20_000,
], ids=["long-argument", "long-command", "long-function-name", "nested-substitutions",
        "unclosed-braces", "unclosed-parens"])
def test_a_pathological_line_is_decided_quickly(command, tmp_path):
    _, seconds = _timed(command, tmp_path)
    assert seconds < 5, f"{seconds:.1f}s"


def test_a_long_heredoc_is_decided_quickly(tmp_path):
    body = "\n".join(
        f"    value_{i} = compute(arg_{i}, 'text {i}')  # note {i}" for i in range(2000)
    )
    decision, seconds = _timed(f"cat > gen.py <<'EOF'\n{body}\nEOF", tmp_path)
    assert decision == Decision.ask
    assert seconds < 5, f"{seconds:.1f}s"
