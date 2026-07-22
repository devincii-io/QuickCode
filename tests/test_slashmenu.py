"""Slash-command autocomplete: matching + arg detection."""

from quickcode.ui.slashmenu import (
    SLASH_COMMANDS,
    command_takes_args,
    match_commands,
)


def test_match_filters_by_prefix():
    names = [c[0] for c in match_commands("/mo")]
    assert names == ["/model", "/mode"]  # both start with /mo, in definition order


def test_match_requires_leading_slash():
    assert match_commands("mo") == []
    assert match_commands("") == []


def test_slash_alone_lists_everything():
    assert len(match_commands("/")) == len(SLASH_COMMANDS)


def test_arg_detection():
    assert command_takes_args("/mode") is True
    assert command_takes_args("/clear") is False
