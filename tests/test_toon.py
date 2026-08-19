"""What the model sees when it is handed structured data.

The encoder has one job the tests have to keep honest: a reader must be able
to recover the fields. Every case here is either a shape QuickCode actually
emits (grep matches, search hits, task lists, agent jobs) or a value that
would break the split if it were not quoted -- Windows paths, text containing
the delimiter, strings that read as numbers.

The spec examples are pinned verbatim, because "close enough to TOON" is a
format nobody has an implementation of.
"""

from __future__ import annotations

import pytest

from quickcode.context import toon

BACKSLASH = chr(92)


# ---- the four forms ----


def test_a_uniform_array_declares_its_fields_once_and_counts_its_rows() -> None:
    out = toon.encode({"matches": [
        {"path": "a.py", "line": 1, "text": "x"},
        {"path": "b.py", "line": 2, "text": "y"},
    ]})
    assert out == "matches[2]{path,line,text}:\n  a.py,1,x\n  b.py,2,y"


def test_a_primitive_array_stays_on_its_header_line() -> None:
    assert toon.encode({"alerts": ["frost", "wind"]}) == "alerts[2]: frost,wind"


def test_uniform_nested_objects_fold_into_the_header_and_rows_stay_flat() -> None:
    """The spec's own weather example, byte for byte."""
    out = toon.encode({
        "location": {"city": "Berlin", "country": "DE", "units": "metric"},
        "alerts": ["frost", "wind"],
        "forecast": [
            {"day": "Mon", "temp": {"min": -2, "max": 4}, "condition": "snow", "rainChance": 80},
            {"day": "Tue", "temp": {"min": 1, "max": 7}, "condition": "cloudy", "rainChance": 20},
            {"day": "Wed", "temp": {"min": 3, "max": 11}, "condition": "sunny", "rainChance": 5},
        ],
    })
    assert out == (
        "location:\n"
        "  city: Berlin\n"
        "  country: DE\n"
        "  units: metric\n"
        "alerts[2]: frost,wind\n"
        "forecast[3]{day,temp{min,max},condition,rainChance}:\n"
        "  Mon,-2,4,snow,80\n"
        "  Tue,1,7,cloudy,20\n"
        "  Wed,3,11,sunny,5"
    )


def test_an_object_of_uniform_objects_becomes_a_keyed_table() -> None:
    out = toon.encode({"environments": {
        "production": {"region": "eu-central-1", "replicas": 6, "debug": False},
        "staging": {"region": "eu-central-1", "replicas": 2, "debug": True},
    }})
    assert out == (
        "environments[2:]{region,replicas,debug}:\n"
        "  production: eu-central-1,6,false\n"
        "  staging: eu-central-1,2,true"
    )


def test_rows_that_disagree_fall_back_to_a_list_rather_than_lying_about_shape() -> None:
    out = toon.encode({"items": [{"a": 1}, {"b": 2}]})
    assert out == "items[2]:\n  - a: 1\n  - b: 2"


def test_a_cell_that_holds_a_list_disqualifies_the_table() -> None:
    """A row is flat. A list in a cell has nowhere to go, so no table."""
    out = toon.encode({"items": [{"a": [1, 2]}, {"a": [3]}]})
    assert out.startswith("items[2]:")
    assert "{a}" not in out


def test_mixed_element_types_survive_in_list_form() -> None:
    out = toon.encode({"mixed": [1, {"a": 1, "b": 2}, "x"]})
    assert out == "mixed[3]:\n  - 1\n  - a: 1\n    b: 2\n  - x"


def test_an_empty_object_element_is_a_bare_dash() -> None:
    assert toon.encode({"mixed": [1, {}]}) == "mixed[2]:\n  - 1\n  -"


# ---- counts ----


@pytest.mark.parametrize("n", [0, 1, 7, 200])
def test_the_declared_count_is_the_number_of_rows_a_reader_can_check(n: int) -> None:
    rows = [{"i": i, "v": "x"} for i in range(n)]
    out = toon.encode({"rows": rows})
    assert out.startswith(f"rows[{n}]")
    body = [line for line in out.splitlines()[1:] if line.strip()]
    assert len(body) == n


def test_an_empty_array_says_zero_rather_than_going_missing() -> None:
    assert toon.encode({"none": []}) == "none[0]:"


# ---- quoting: the part that decides whether a row can be split ----


def test_a_value_containing_the_delimiter_is_quoted() -> None:
    out = toon.encode({"rows": [{"text": "a,b"}]})
    assert out.splitlines()[1].strip() == '"a,b"'


def test_a_windows_path_survives_with_its_separators_intact() -> None:
    path = "C:" + BACKSLASH + "Users" + BACKSLASH + "a.py"
    out = toon.encode({"rows": [{"path": path, "line": 3}]})
    row = out.splitlines()[1].strip()
    assert row == path + ",3"
    assert row.split(",")[0] == path


def test_a_colon_in_a_value_does_not_need_quoting_because_columns_are_positional() -> None:
    """The whole point of the delimiter: `path:line:text` was ambiguous, this is not."""
    out = toon.encode({"rows": [{"text": "http://x/y", "line": 1}]})
    assert out.splitlines()[1].strip() == "http://x/y,1"


@pytest.mark.parametrize("value", ["true", "false", "null", "42", "-1.5", "1e9"])
def test_a_string_that_reads_as_a_literal_is_quoted_so_it_reads_back_as_text(value: str) -> None:
    out = toon.encode({"v": value})
    assert out == f'v: "{value}"'


@pytest.mark.parametrize("value", [True, False, None, 42, -1.5])
def test_the_real_literals_are_not_quoted(value: object) -> None:
    out = toon.encode({"v": value})
    assert '"' not in out


def test_leading_and_trailing_space_is_preserved_by_quoting() -> None:
    assert toon.encode({"v": "  x  "}) == 'v: "  x  "'


def test_an_empty_string_is_visible_rather_than_an_empty_column() -> None:
    assert toon.encode({"v": ""}) == 'v: ""'


def test_a_newline_inside_a_value_cannot_forge_a_row() -> None:
    out = toon.encode({"rows": [{"text": "a\nb.py,9,evil"}]})
    assert len(out.splitlines()) == 2
    assert BACKSLASH + "n" in out


def test_a_quote_inside_a_value_is_escaped() -> None:
    out = toon.encode({"v": 'say "hi", ok'})
    assert out == 'v: "say ' + BACKSLASH + '"hi' + BACKSLASH + '", ok"'


def test_a_key_that_would_break_the_header_is_quoted() -> None:
    out = toon.encode({"rows": [{"a,b": 1}, {"a,b": 2}]})
    assert out.splitlines()[0] == 'rows[2]{"a,b"}:'


def test_a_value_that_looks_like_structure_is_quoted() -> None:
    for value in ["- item", "# heading", "[1]", "{a}"]:
        assert toon.encode({"v": value}) == f'v: "{value}"'


# ---- delimiters ----


def test_the_tab_delimiter_changes_what_has_to_be_quoted() -> None:
    rows = [{"text": "a,b"}]
    assert toon.encode({"r": rows}, delimiter=toon.TAB).splitlines()[1].strip() == "a,b"
    assert toon.encode({"r": rows}).splitlines()[1].strip() == '"a,b"'


def test_a_tab_inside_a_value_is_escaped_under_every_delimiter() -> None:
    out = toon.encode({"v": "a\tb"}, delimiter=toon.TAB)
    assert "\t" not in out.split(": ", 1)[1]


# ---- shape and determinism ----


def test_the_same_input_always_encodes_to_the_same_bytes() -> None:
    """Prompt caching depends on this; so does any test that pins output."""
    data = {"a": [{"x": 1, "y": "two"}], "b": {"c": None}}
    assert toon.encode(data) == toon.encode(dict(data))


def test_key_order_is_the_caller_s_order_not_sorted() -> None:
    assert toon.encode({"z": 1, "a": 2}) == "z: 1\na: 2"


def test_a_root_array_needs_no_key() -> None:
    assert toon.encode([{"a": 1}, {"a": 2}]) == "[2]{a}:\n  1\n  2"


def test_a_bare_scalar_is_just_itself() -> None:
    assert toon.encode("hello") == "hello"


def test_a_cycle_is_reported_rather_than_blowing_the_stack() -> None:
    node: dict = {"name": "n"}
    node["child"] = node
    out = toon.encode(node)
    assert "too deeply nested" in out


def test_the_fence_names_the_format_once() -> None:
    out = toon.fenced({"a": 1})
    assert out.startswith("```toon\n")
    assert out.endswith("\n```")
    assert "a: 1" in out


# ---- the shapes QuickCode actually emits ----


def test_grep_matches_are_splittable_where_the_old_rendering_was_not() -> None:
    """`path:line:text` is not cheaper than this -- it is *wrong* on Windows.

    A drive letter puts a colon in the first field, so a reader splitting on
    the first colon gets `C` as the path. TOON costs two spaces of indent per
    row and buys back an unambiguous split and a row count.
    """
    drive = "C:" + BACKSLASH + "src" + BACKSLASH + "mod.py"
    rows = [{"path": drive, "line": 12, "text": "def f():"}]
    encoded = toon.encode({"matches": rows})
    assert encoded.splitlines()[0] == "matches[1]{path,line,text}:"
    assert encoded.splitlines()[1].strip().split(",") == [drive, "12", "def f():"]

    old = f"{drive}:12:def f():"
    assert old.split(":")[0] == "C"  # the bug this replaces


def test_a_task_list_keeps_the_fields_the_checklist_used_to_drop() -> None:
    out = toon.encode({"tasks": [
        {"id": "T1", "status": "done", "subject": "a", "blocked_by": "", "owner": "me"},
        {"id": "T2", "status": "todo", "subject": "b", "blocked_by": "T1", "owner": ""},
    ]})
    assert out.splitlines()[0] == "tasks[2]{id,status,subject,blocked_by,owner}:"
    assert out.splitlines()[2] == '  T2,todo,b,T1,""'
