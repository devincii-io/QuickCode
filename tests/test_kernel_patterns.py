"""The one reading of a ``tools:`` / ``spawns:`` / ``models:`` entry.

The resolver, the used-by index and the workbench each used to carry their own
copy of these two questions. They agreed, which is the point of pinning them
here: the day one of them changes, all three move together.
"""

from __future__ import annotations

import pytest

from quickcode.kernel.patterns import is_glob, pattern_matches


@pytest.mark.parametrize(("pattern", "expected"), [
    ("read", False),
    ("", False),
    ("mcp__files__read", False),
    ("task_*", True),
    ("mcp__files__*", True),
    ("bash_?", True),
    ("[rw]*", True),
])
def test_is_glob_names_the_entries_that_can_match_more_than_themselves(pattern, expected):
    assert is_glob(pattern) is expected


@pytest.mark.parametrize(("pattern", "name", "expected"), [
    ("read", "read", True),
    ("read", "reader", False),
    ("task_*", "task_create", True),
    ("task_*", "task", False),
    ("mcp__files__*", "mcp__files__read", True),
    ("mcp__files__*", "mcp__other__read", False),
    ("Read", "read", False),
    ("*", "anything", True),
])
def test_pattern_matches_is_literal_or_glob_and_case_sensitive(pattern, name, expected):
    assert pattern_matches(pattern, name) is expected
