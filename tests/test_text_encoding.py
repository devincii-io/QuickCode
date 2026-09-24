"""A text file a person edits reads as its text, whichever Windows editor saved it.

Notepad, PowerShell's ``Set-Content`` and Visual Studio save UTF-8 with a
byte-order mark, or UTF-16. Read as plain UTF-8, the mark stayed in the text:
``AGENTS.md`` reached the system prompt starting with U+FEFF, and an authored
plugin's first line was no longer ``---``, so its frontmatter -- its ``kind``,
its ``name``, a command tool's argv -- was not read at all. A UTF-16 file did
not decode, and was skipped. ``tests/test_settings_encoding.py`` is the same
story for JSON; ``quickcode/textio.py`` is now the one decoder for both.
"""

from __future__ import annotations

import codecs

import pytest

from quickcode import config, textio
from quickcode.kernel.authoring import discovery, store
from quickcode.security import trust
from quickcode.subagents.definitions import load_defs
from tests.test_authoring import ECHO, echo_tool

ENCODINGS = {
    "utf-8": lambda text: text.encode("utf-8"),
    "utf-8-bom": lambda text: text.encode("utf-8-sig"),
    "utf-16-le": lambda text: codecs.BOM_UTF16_LE + text.encode("utf-16-le"),
    "utf-16-be": lambda text: codecs.BOM_UTF16_BE + text.encode("utf-16-be"),
    "utf-32": lambda text: codecs.BOM_UTF32_LE + text.encode("utf-32-le"),
}


@pytest.fixture(params=sorted(ENCODINGS))
def encode(request):
    return ENCODINGS[request.param]


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A trusted project with an empty plugins directory, user scope in tmp_path."""
    home = tmp_path / "home" / ".quickcode"
    (home / "plugins").mkdir(parents=True)
    project = tmp_path / "proj"
    (project / ".quickcode" / "plugins").mkdir(parents=True)
    monkeypatch.setattr(config, "CONFIG_DIR", home)
    monkeypatch.setattr(trust, "is_trusted", lambda cwd: True)
    return project


def test_line_endings_read_as_python_text_mode_reads_them(tmp_path):
    """The readers this replaces were ``Path.read_text``, which turns every
    ``\\r\\n`` and lone ``\\r`` into ``\\n``; the prompt bytes must not change
    for a file that has no mark."""
    path = tmp_path / "x.md"
    path.write_bytes(b"one\r\ntwo\rthree\n")
    assert textio.read_text(path) == "one\ntwo\nthree\n"


def test_text_that_is_not_utf8_and_has_no_mark_is_refused(tmp_path):
    path = tmp_path / "x.md"
    path.write_bytes("café".encode("latin-1"))
    with pytest.raises(UnicodeDecodeError):
        textio.read_text(path)


def test_project_instructions_read_as_their_text(tmp_path, encode):
    (tmp_path / "AGENTS.md").write_bytes(encode("# Rules\r\nUse café tabs.\r\n"))

    text, name = config._load_project_instructions(tmp_path)

    assert name == "AGENTS.md"
    assert text == "# Rules\nUse café tabs.\n"


def test_an_authored_plugin_keeps_its_frontmatter(sandbox, encode):
    source = echo_tool(ECHO, [])
    (sandbox / ".quickcode" / "plugins" / "echo-args.md").write_bytes(encode(source))

    found = discovery.discover(sandbox)

    assert [p.id for p in found.plugins] == ["tool.echo-args"], found.problems
    _path, text, problems = store.read_source(sandbox, "tool.echo-args")
    assert text == source and not problems


def test_the_trust_gate_reads_an_authored_file_as_the_loader_does(sandbox, encode):
    """Only a command tool is part of the trust hash. A gate that could not
    read the kind hashed the file to be safe; it now reads what the loader
    reads, so an agent file saved as UTF-16 is not mistaken for a tool."""
    plugins = sandbox / ".quickcode" / "plugins"
    (plugins / "echo-args.md").write_bytes(encode(echo_tool(ECHO, [])))
    (plugins / "helper.md").write_bytes(
        encode("---\nkind: agent\nname: helper\ndescription: Helps.\n---\nHelp.\n"))

    assert sorted(trust.project_command_tools(sandbox)) == ["echo-args.md"]


def test_a_subagent_definition_keeps_its_frontmatter(tmp_path, encode):
    agents = tmp_path / ".quickcode" / "agents"
    agents.mkdir(parents=True)
    (agents / "reviewer.md").write_bytes(
        encode("---\r\nname: reviewer\r\ntools: [read, grep]\r\n---\r\nReview.\r\n"))

    defs = load_defs(tmp_path)

    assert defs["reviewer"].tools == ["read", "grep"]
    assert defs["reviewer"].prompt_body == "Review."
