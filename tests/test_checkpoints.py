"""File checkpoints, recorded by a real agent loop and rewound for real.

Every recording test drives a real ``AgentInstance`` with the real ``write``
and ``edit`` tools, a real ``PermissionEngine`` and the session's real hook
list (``session_hooks``), so the claim under test is the one a user relies on:
after the agent changed these files, a rewind puts back exactly the bytes
that were there before the turn.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import BaseModel

from quickcode.checkpoints import paths, rewind
from quickcode.checkpoints.events import FileCheckpointed
from quickcode.checkpoints.hook import CheckpointHook, file_targets
from quickcode.checkpoints.recorder import Checkpointer
from quickcode.checkpoints.snapshot import digest
from quickcode.checkpoints.store import CheckpointStore, FileEntry
from quickcode.core.agent import AgentInstance, PermissionOutcome
from quickcode.core.events import (
    AssembledToolCall,
    TextDelta,
    ToolCallEnd,
    ToolCallStart,
    TurnDone,
)
from quickcode.core.history import History
from quickcode.core.permissions import Mode, PermissionEngine, PermissionSpec, Rules
from quickcode.hooks.plugin import child_hooks, session_hooks
from quickcode.session.recorder import TranscriptRecorder
from quickcode.session.store import SessionStore, purge_sessions
from quickcode.tools.base import ReadRegistry, Tool, ToolCtx, ToolResult
from quickcode.tools.registry import ToolRegistry, default_registry

CONV = "conv-ckpt"


class Scripted:
    """One round of tool calls per entry in ``rounds``, then a plain answer."""

    def __init__(self, rounds: list[list[AssembledToolCall]] | None = None) -> None:
        self.rounds = list(rounds or [])

    def then(self, *calls: AssembledToolCall) -> Scripted:
        self.rounds.append(list(calls))
        return self

    async def stream_chat(self, req) -> AsyncIterator:
        if self.rounds:
            for call in self.rounds.pop(0):
                yield ToolCallStart(call.id, call.name)
                yield ToolCallEnd(call.id, call.name, call.arguments)
            yield TurnDone("tool_calls")
            return
        yield TextDelta("done")
        yield TurnDone("stop")

    async def list_models(self):
        return []


class BlobInput(BaseModel):
    file_path: str
    hex: str = ""
    delete: bool = False


class BlobTool(Tool[BlobInput]):
    """A plugin-shaped tool: it writes raw bytes and declares its path target.

    Stands in for any tool that is not ``write`` or ``edit`` -- nothing about
    checkpointing it is special-cased."""

    name: ClassVar[str] = "blob"
    description: ClassVar[str] = "Write raw bytes."
    permission = PermissionSpec(mutates=True, target_field="file_path", path_target=True)
    Input = BlobInput

    async def run(self, input: BlobInput, ctx: ToolCtx) -> ToolResult:  # noqa: A002
        path = Path(input.file_path)
        if input.delete:
            path.unlink()
        else:
            path.write_bytes(bytes.fromhex(input.hex))
        return ToolResult(content="ok")


_ids = iter(range(1, 1_000_000))


def call(name: str, **args) -> AssembledToolCall:
    return AssembledToolCall(f"call_{next(_ids)}", name, json.dumps(args))


def read(path: Path) -> AssembledToolCall:
    return call("read", file_path=str(path))


def edit(path: Path, old: str, new: str) -> AssembledToolCall:
    return call("edit", file_path=str(path), old_string=old, new_string=new)


def write(path: Path, content: str) -> AssembledToolCall:
    return call("write", file_path=str(path), content=content)


def blob(path: Path, data: bytes = b"", delete: bool = False) -> AssembledToolCall:
    return call("blob", file_path=str(path), hex=data.hex(), delete=delete)


class Session:
    """A conversation as the app runs one: session hooks, recorder, session log."""

    def __init__(self, root: Path, *, mode: Mode = Mode.auto_edit, allow: bool = True,
                 conv_id: str = CONV, tools: tuple[Tool, ...] = ()) -> None:
        self.root = root
        self.conv_id = conv_id
        self.provider = Scripted()
        registry = ToolRegistry([*default_registry().tools.values(), BlobTool(), *tools])
        self.asked: list = []

        async def answer(req):
            self.asked.append(req)
            return PermissionOutcome(allow=allow)

        self.agent = AgentInstance(
            name="main", provider=self.provider, registry=registry,
            history=History("SYS"),
            ctx=ToolCtx(cwd=root, read_registry=ReadRegistry(), platform=sys.platform),
            permissions=PermissionEngine(mode, Rules(), root,
                                         specs=registry.permission_specs()),
            model="test/model", permission_cb=answer,
            hooks=session_hooks(root, session_id=conv_id, trusted=False),
        )

    def turn(self, *rounds: list[AssembledToolCall], text: str = "go") -> None:
        """One recorded turn, the way ``quickcode -p`` runs one: a store and a
        recorder per turn, as each process has its own."""
        self.provider.rounds.extend(rounds)
        recorder = TranscriptRecorder(self.store, persisted=len(self.agent.history.messages))
        asyncio.run(recorder.record_turn(self.agent, text))

    @property
    def store(self) -> SessionStore:
        return SessionStore(self.root, self.conv_id)

    @property
    def checkpoints(self) -> CheckpointStore:
        return CheckpointStore(self.root, self.conv_id)

    def events(self, kind: str) -> list[dict]:
        return [e for e in self.store.load_events() if e["type"] == kind]

    def tool_results(self) -> list[str]:
        return [m.content for m in self.agent.history.messages if m.role == "tool"]


def files_of(store: CheckpointStore) -> dict[int, list[str]]:
    return {cp.turn: [e.path for e in cp.files] for cp in store.load().turns}


# ---------------------------------------------------------------- recording


def test_an_edit_saves_the_bytes_the_file_had_before_the_turn(tmp_path):
    f = tmp_path / "app.py"
    f.write_text("x = 1\n", encoding="utf-8")
    s = Session(tmp_path)
    s.turn([read(f)], [edit(f, "x = 1", "x = 2")])

    [cp] = s.checkpoints.load().turns
    [entry] = cp.files
    assert (cp.turn, entry.path, entry.change) == (1, "app.py", "modified")
    assert s.checkpoints.read_blob(entry.before) == b"x = 1\n"
    assert (entry.added, entry.removed, entry.tool) == (1, 1, "edit")
    [logged] = s.events("checkpoint")
    assert (logged["turn"], logged["path"], logged["tool"]) == (1, "app.py", "edit")
    assert "x = 1" not in json.dumps(logged), "the log names the file, never its contents"


def test_several_edits_in_one_turn_keep_the_original_pre_turn_bytes(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("alpha\nbeta\n", encoding="utf-8")
    s = Session(tmp_path)
    s.turn([read(f)], [edit(f, "alpha", "ALPHA")], [edit(f, "beta", "BETA")],
           [write(f, "rewritten\n")])

    [entry] = s.checkpoints.load().turns[0].files
    assert s.checkpoints.read_blob(entry.before) == b"alpha\nbeta\n"
    assert len(entry.calls) == 3
    assert len(s.events("checkpoint")) == 1, "one checkpoint per file per turn"

    rewind.apply(s.checkpoints, 1)
    assert f.read_bytes() == b"alpha\nbeta\n"


def test_each_turn_saves_its_own_before_and_turn_numbers_match_the_log(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("v1\n", encoding="utf-8")
    s = Session(tmp_path)
    s.turn([read(f)], [edit(f, "v1", "v2")])
    s.turn()                                  # a turn that changes nothing
    s.turn([edit(f, "v2", "v3")])

    turns = s.checkpoints.load().turns
    assert [cp.turn for cp in turns] == [1, 3]
    assert [e["turn"] for e in s.events("user_message")] == [1, 2, 3]
    assert s.checkpoints.read_blob(turns[1].files[0].before) == b"v2\n"

    rewind.apply(s.checkpoints, 3)
    assert f.read_bytes() == b"v2\n"
    rewind.apply(s.checkpoints, 1)
    assert f.read_bytes() == b"v1\n"


def test_a_reopened_conversation_carries_on_the_log_s_turn_numbers(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("one\n", encoding="utf-8")
    first = Session(tmp_path)
    first.turn([read(f)], [edit(f, "one", "two")])
    first.turn()

    again = Session(tmp_path)                 # a new process, same conversation
    again.turn([read(f)], [edit(f, "two", "three")])
    assert [cp.turn for cp in again.checkpoints.load().turns] == [1, 3]


def test_a_file_the_turn_created_is_deleted_by_the_rewind(tmp_path):
    f = tmp_path / "pkg" / "new_module.py"
    s = Session(tmp_path)
    s.turn([write(f, "print('hi')\n")])

    [entry] = s.checkpoints.load().turns[0].files
    assert (entry.path, entry.before, entry.change) == ("pkg/new_module.py", None, "created")
    assert s.events("checkpoint")[0]["created"] is True

    result = rewind.apply(s.checkpoints, 1)
    assert not f.exists()
    assert [f.action for f in result.record.files] == ["deleted"]


@pytest.mark.parametrize("original", [
    pytest.param(b"\xef\xbb\xbfline one\r\nline two\r\n", id="utf8-bom-crlf"),
    pytest.param("Größe\r\n".encode("cp1252"), id="cp1252-crlf"),
    pytest.param(b"\xff\xfeh\x00i\x00\r\x00\n\x00", id="utf16-bom"),
    pytest.param(b"no newline at the end", id="no-final-newline"),
])
def test_a_rewind_restores_text_byte_for_byte(tmp_path, original):
    f = tmp_path / "doc.txt"
    f.write_bytes(original)
    s = Session(tmp_path)
    s.turn([read(f)], [write(f, "replaced\n")])
    assert f.read_bytes() != original

    rewind.apply(s.checkpoints, 1)
    assert f.read_bytes() == original


def test_binary_files_and_deleted_then_recreated_files_come_back_exactly(tmp_path):
    png = tmp_path / "icon.png"
    original = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(256))
    png.write_bytes(original)
    gone = tmp_path / "gone.bin"
    gone.write_bytes(b"\x00\x01\x02")
    s = Session(tmp_path)
    s.turn([blob(png, delete=True)], [blob(png, b"\x00new")], [blob(gone, delete=True)])

    by_path = {e.path: e for e in s.checkpoints.load().turns[0].files}
    assert by_path["icon.png"].change == "modified"
    assert by_path["icon.png"].binary is True
    assert by_path["gone.bin"].change == "deleted"

    preview = rewind.preview(s.checkpoints, 1)
    actions = {row["path"]: row["action"] for row in preview["files"]}
    assert actions == {"gone.bin": "create", "icon.png": "restore"}
    assert all(row["omitted"] == "binary" for row in preview["files"])

    rewind.apply(s.checkpoints, 1)
    assert png.read_bytes() == original
    assert gone.read_bytes() == b"\x00\x01\x02"


def test_a_failed_or_refused_call_records_nothing(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("same\n", encoding="utf-8")
    s = Session(tmp_path)
    s.turn([read(f)], [edit(f, "not there", "x")])
    assert "old_string not found" in s.tool_results()[-1]

    refused = Session(tmp_path, mode=Mode.ask, allow=False, conv_id="conv-refused")
    refused.turn([read(f)], [edit(f, "same", "changed")])
    assert refused.asked, "the call was put to the user"

    assert not s.checkpoints.exists()
    assert not refused.checkpoints.exists()
    assert f.read_text(encoding="utf-8") == "same\n"


def test_files_outside_the_project_and_bash_are_not_checkpointed(tmp_path):
    project, outside = tmp_path / "project", tmp_path / "outside.txt"
    project.mkdir()
    s = Session(project, mode=Mode.yolo)
    s.turn([write(outside, "not ours\n")])
    assert outside.exists()
    assert not s.checkpoints.exists()

    bash = default_registry().get("bash")
    assert file_targets(bash, {"command": "echo hi > a.txt"}, project) == []
    assert "bash" in rewind.listing(s.checkpoints)["untracked"]


def test_a_subagent_s_edits_land_in_the_turn_that_spawned_it(tmp_path):
    f = tmp_path / "child.txt"
    f.write_text("parent wrote this\n", encoding="utf-8")
    s = Session(tmp_path)
    s.turn()                                   # turn 1, nothing changed
    parent_hook = next(h for h in s.agent.hooks if isinstance(h, CheckpointHook))
    parent_hook.checkpointer.begin_turn()      # turn 2 is running

    registry = default_registry()
    child = AgentInstance(
        name="explore-1", provider=Scripted([[read(f)], [write(f, "child wrote this\n")]]),
        registry=registry, history=History("SYS"),
        ctx=ToolCtx(cwd=tmp_path, read_registry=ReadRegistry(), platform=sys.platform),
        permissions=PermissionEngine(Mode.auto_edit, Rules(), tmp_path,
                                     specs=registry.permission_specs()),
        model="test/model", permission_cb=None, hooks=child_hooks(s.agent.hooks),
    )
    asyncio.run(child.run_turn("delegated"))

    [cp] = s.checkpoints.load().turns
    assert cp.turn == 2, "a delegation is not a user turn"
    assert (cp.files[0].path, cp.files[0].agent) == ("child.txt", "explore-1")


def test_an_authored_command_tool_s_path_parameters_are_checkpointed(tmp_path, monkeypatch):
    """A program may write and then fail: its change is recorded all the same."""
    import quickcode.config as config_module
    from quickcode.kernel.authoring import discovery
    from quickcode.security import trust

    home = tmp_path / "home" / ".quickcode"
    (home / "plugins").mkdir(parents=True)
    project = tmp_path / "proj"
    (project / ".quickcode" / "plugins").mkdir(parents=True)
    monkeypatch.setattr(config_module, "CONFIG_DIR", home)
    monkeypatch.setattr(trust, "is_trusted", lambda cwd: True)
    shout = ("import pathlib, sys; p = pathlib.Path(sys.argv[1]); "
             "p.write_text(p.read_text().upper()); sys.exit(1)")
    (project / ".quickcode" / "plugins" / "shout.md").write_text("\n".join([
        "---", "kind: tool", "name: shout", "description: Uppercase a file.", "---", "",
        "```json params", json.dumps([{"name": "target", "type": "path", "required": True}]),
        "```", "", "```json argv", json.dumps([sys.executable, "-c", shout, "{target}"]),
        "```", ""]), encoding="utf-8")
    [plugin] = discovery.discover(project).plugins
    f = project / "quiet.txt"
    f.write_text("quiet\n", encoding="utf-8")

    s = Session(project, tools=(plugin.to_tool(),))
    s.turn([call("shout", target="quiet.txt")])
    assert f.read_text(encoding="utf-8") == "QUIET\n"
    assert s.tool_results()[0].startswith("[error]"), "it exited 1"
    [entry] = s.checkpoints.load().turns[0].files
    assert (entry.path, entry.tool) == ("quiet.txt", "shout")

    rewind.apply(s.checkpoints, 1)
    assert f.read_text(encoding="utf-8") == "quiet\n"


# ---------------------------------------------------------------- conflicts


def test_a_file_changed_after_the_recorded_edit_is_a_conflict(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("before\n", encoding="utf-8")
    s = Session(tmp_path)
    s.turn([read(f)], [edit(f, "before", "agent")])
    f.write_text("the user kept typing\n", encoding="utf-8")

    preview = rewind.preview(s.checkpoints, 1)
    [row] = preview["files"]
    assert [c["kind"] for c in row["conflicts"]] == ["modified"]
    assert preview["conflicts"] == ["a.txt"]
    assert "-the user kept typing" in row["diff"] and "+before" in row["diff"]

    with pytest.raises(rewind.RewindError) as refused:
        rewind.apply(s.checkpoints, 1)
    assert refused.value.status == 409
    assert f.read_text(encoding="utf-8") == "the user kept typing\n", "a refusal writes nothing"

    result = rewind.apply(s.checkpoints, 1, force=True)
    assert f.read_text(encoding="utf-8") == "before\n"
    [done] = result.record.files
    assert result.record.forced and done.backup
    assert s.checkpoints.read_blob(done.replaced) == b"the user kept typing\n", (
        "what a forced rewind overwrote is kept")


def test_a_change_between_two_recorded_turns_is_reported(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("1\n", encoding="utf-8")
    s = Session(tmp_path)
    s.turn([read(f)], [edit(f, "1", "2")])
    f.write_text("by hand\n", encoding="utf-8")
    s.turn([read(f)], [edit(f, "by hand", "3")])

    [row] = rewind.preview(s.checkpoints, 1)["files"]
    assert [c["kind"] for c in row["conflicts"]] == ["intervening"]
    assert rewind.preview(s.checkpoints, 2)["files"][0]["conflicts"] == []


def test_selected_files_only_and_unknown_selections_are_refused(tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("a\n", encoding="utf-8")
    b.write_text("b\n", encoding="utf-8")
    s = Session(tmp_path)
    s.turn([read(a), read(b)], [edit(a, "a", "A")], [edit(b, "b", "B")])

    with pytest.raises(rewind.RewindError) as unknown:
        rewind.apply(s.checkpoints, 1, ["c.txt"])
    assert unknown.value.status == 400

    rewind.apply(s.checkpoints, 1, ["b.txt"])
    assert (a.read_text(encoding="utf-8"), b.read_text(encoding="utf-8")) == ("A\n", "b\n")
    # b's entries are spent; a is still rewindable on its own.
    assert [row["path"] for row in rewind.preview(s.checkpoints, 1)["files"]] == ["a.txt"]


def test_a_rewind_leaves_the_chain_consistent_for_a_later_one(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("0\n", encoding="utf-8")
    s = Session(tmp_path)
    s.turn([read(f)], [edit(f, "0", "1")])
    s.turn([edit(f, "1", "2")])
    rewind.apply(s.checkpoints, 2)
    assert f.read_text(encoding="utf-8") == "1\n"
    s.turn([read(f)], [edit(f, "1", "4")])

    [row] = rewind.preview(s.checkpoints, 1)["files"]
    assert row["conflicts"] == [], "the rewound turn no longer takes part"
    rewind.apply(s.checkpoints, 1)
    assert f.read_text(encoding="utf-8") == "0\n"


def test_the_model_is_told_about_a_rewind_on_its_next_turn(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("mine\n", encoding="utf-8")
    s = Session(tmp_path)
    s.turn([read(f)], [edit(f, "mine", "yours")])
    rewind.apply(s.checkpoints, 1)
    s.turn()
    s.turn()

    reminders = [e["text"] for e in s.events("context_injection") if "rewound" in e["text"]]
    assert len(reminders) == 1, "told once"
    assert "a.txt" in reminders[0] and "before turn 1" in reminders[0]


# ------------------------------------------------------------ size and eviction


def test_oldest_turns_are_evicted_first_and_say_so(tmp_path, monkeypatch):
    monkeypatch.setattr("quickcode.checkpoints.store.DEFAULT_MAX_BYTES", 250)
    files = [tmp_path / f"f{i}.txt" for i in range(3)]
    for i, f in enumerate(files):
        f.write_text(chr(ord("a") + i) * 99 + "\n", encoding="utf-8")
    s = Session(tmp_path)
    for f in files:
        s.turn([read(f)], [write(f, "new\n")])

    store = s.checkpoints
    entries = [cp.files[0] for cp in store.load().turns]
    assert [(e.restorable, e.reason) for e in entries] == [
        (False, "evicted"), (True, ""), (True, "")]
    assert store.load().used_bytes() <= 250
    assert len(list(store.blob_dir.iterdir())) == 2, "the evicted blob is deleted"

    result = rewind.apply(store, 1)
    assert result.skipped == [{"path": "f0.txt", "reason": "evicted"}]
    assert files[0].read_text(encoding="utf-8") == "new\n", "never restored from nothing"
    assert files[1].read_text(encoding="utf-8") == "b" * 99 + "\n"


def test_a_file_over_the_per_file_limit_is_marked_not_restorable(tmp_path, monkeypatch):
    monkeypatch.setattr("quickcode.checkpoints.store.DEFAULT_MAX_FILE_BYTES", 10)
    f = tmp_path / "big.txt"
    f.write_text("0123456789abcdef\n", encoding="utf-8")
    s = Session(tmp_path)
    s.turn([read(f)], [write(f, "small\n")])

    [entry] = s.checkpoints.load().turns[0].files
    assert (entry.restorable, entry.reason) == (False, "too_large")
    assert s.events("checkpoint")[0]["restorable"] is False
    [row] = rewind.preview(s.checkpoints, 1)["files"]
    assert (row["restorable"], row["reason"]) == (False, "too_large")


def test_a_damaged_blob_is_never_restored(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("good\n", encoding="utf-8")
    s = Session(tmp_path)
    s.turn([read(f)], [edit(f, "good", "new")])
    [entry] = s.checkpoints.load().turns[0].files
    (s.checkpoints.blob_dir / entry.before).write_bytes(b"tampered")

    result = rewind.apply(s.checkpoints, 1)
    assert result.skipped == [{"path": "a.txt", "reason": "damaged"}]
    assert f.read_text(encoding="utf-8") == "new\n"


# ---------------------------------------------------------------- path safety


@pytest.mark.parametrize("rel", ["../escape.txt", "/etc/passwd", "a/../../b", "", "a//b",
                                 "a\\b", ".quickcode/checkpoints/x/index.json",
                                 ".quickcode/worktrees/worker-1-ab12/a.txt"])
def test_a_path_that_is_not_plainly_inside_the_project_is_refused(tmp_path, rel):
    assert paths.target(paths.real_root(tmp_path), rel) is None


def test_a_subagents_scratch_worktree_is_not_the_project(tmp_path):
    # An isolated child edits its own checkout under .quickcode/worktrees, and
    # that checkout is removed when its run ends -- the work comes back as a
    # branch. Recorded, a rewind would recreate its files in a directory that
    # no longer belongs to anything.
    tree = tmp_path / ".quickcode" / "worktrees" / "worker-1-ab12"
    tree.mkdir(parents=True)
    (tree / "a.txt").write_text("child's\n", encoding="utf-8")
    (tmp_path / "a.txt").write_text("project's\n", encoding="utf-8")
    cp = Checkpointer(tmp_path, CONV)
    assert [c.rel for c in cp.capture([tree / "a.txt", tmp_path / "a.txt"])] == ["a.txt"]


@pytest.mark.parametrize("rel", [".git/hooks/pre-commit", ".git/config", "sub/.git/config",
                                 ".GIT/hooks/post-checkout", "GIT~1/config"])
def test_a_rewind_never_writes_into_a_repository_s_git_directory(tmp_path, rel):
    """The index is a file in the project, so a cloned repository can ship one --
    with a blob and a session to hang it on. Everything else it could make a
    rewind write, it could have committed; a hook or a ``core.fsmonitor`` in
    ``.git`` it could not, and git runs those on the user's next command."""
    payload = b"#!/bin/sh\necho owned\n"
    store = CheckpointStore(tmp_path, CONV)
    with store.transaction() as index:
        store._put_blob(digest(payload), payload)
        index.open_turn(1).files.append(FileEntry(path=rel, before=digest(payload), after=None))

    preview = rewind.preview(store, 1)
    [row] = preview["files"]
    assert row["blocked"] and "git" in row["blocked"]
    result = rewind.apply(store, 1, force=True)
    assert result.record is None and result.skipped[0]["path"] == rel
    assert not (tmp_path / rel).exists()


def test_a_file_that_only_looks_like_git_metadata_is_still_rewound(tmp_path):
    before = b"keep me\n"
    store = CheckpointStore(tmp_path, CONV)
    with store.transaction() as index:
        store._put_blob(digest(before), before)
        index.open_turn(1).files.append(
            FileEntry(path="docs/.gitignore", before=digest(before), after=None))

    rewind.apply(store, 1)
    assert (tmp_path / "docs" / ".gitignore").read_bytes() == before


def _symlink(link: Path, to: Path) -> None:
    try:
        link.symlink_to(to, target_is_directory=to.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("this platform or account cannot create symlinks")


def test_a_rewind_never_writes_through_a_link_made_after_the_checkpoint(tmp_path):
    project, elsewhere = tmp_path / "project", tmp_path / "elsewhere"
    (project / "sub").mkdir(parents=True)
    elsewhere.mkdir()
    f = project / "sub" / "a.txt"
    f.write_text("original\n", encoding="utf-8")
    s = Session(project)
    s.turn([read(f)], [edit(f, "original", "edited")])

    (project / "sub").rename(project / "moved")
    _symlink(project / "sub", elsewhere)
    (elsewhere / "a.txt").write_text("edited\n", encoding="utf-8")

    result = rewind.apply(s.checkpoints, 1, force=True)
    assert result.record is None
    assert "link" in result.skipped[0]["reason"]
    assert (elsewhere / "a.txt").read_text(encoding="utf-8") == "edited\n"


def test_an_edit_through_a_link_inside_the_project_is_recorded_at_its_real_path(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("x\n", encoding="utf-8")
    _symlink(tmp_path / "alias.txt", real)
    s = Session(tmp_path)
    s.turn([read(tmp_path / "alias.txt")], [edit(tmp_path / "alias.txt", "x", "y")])

    assert files_of(s.checkpoints) == {1: ["real.txt"]}
    rewind.apply(s.checkpoints, 1)
    assert real.read_text(encoding="utf-8") == "x\n"
    assert (tmp_path / "alias.txt").is_symlink()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_a_rewind_keeps_the_mode_of_the_file_it_replaces(tmp_path):
    script = tmp_path / "run.sh"
    script.write_text("echo one\n", encoding="utf-8")
    script.chmod(0o755)
    s = Session(tmp_path)
    s.turn([read(script)], [edit(script, "one", "two")])
    rewind.apply(s.checkpoints, 1)
    assert script.read_text(encoding="utf-8") == "echo one\n"
    assert script.stat().st_mode & 0o777 == 0o755


# ---------------------------------------------------------------- housekeeping


def test_the_store_keeps_itself_out_of_git_and_goes_with_its_session(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("a\n", encoding="utf-8")
    s = Session(tmp_path)
    s.turn([read(f)], [edit(f, "a", "b")])

    guard = tmp_path / ".quickcode" / "checkpoints" / ".gitignore"
    assert guard.read_text(encoding="utf-8").splitlines()[-1] == "*"
    result = purge_sessions(tmp_path, [CONV])
    assert result.checkpoints == [CONV]
    assert not s.checkpoints.dir.exists()


def test_git_ignores_the_copies_even_under_a_gitignore_that_predates_them(tmp_path):
    def vcs(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, capture_output=True,
                              text=True, check=False)

    if shutil.which("git") is None or vcs("init", "-q").returncode != 0:
        pytest.skip("git not available")
    old_guard = tmp_path / ".quickcode" / ".gitignore"
    old_guard.parent.mkdir()
    old_guard.write_text("sessions/\ntasks/\nartifacts/\n", encoding="utf-8")
    secret = tmp_path / ".env.example"
    secret.write_text("TOKEN=abc\n", encoding="utf-8")
    s = Session(tmp_path, mode=Mode.yolo)
    s.turn([read(secret)], [edit(secret, "abc", "def")])
    assert s.checkpoints.exists()

    vcs("add", "-A")
    staged = vcs("diff", "--cached", "--name-only").stdout.splitlines()
    assert ".env.example" in staged
    assert not [p for p in staged if p.startswith(".quickcode/checkpoints/")]


def test_a_damaged_index_is_set_aside_not_trusted(tmp_path):
    store = CheckpointStore(tmp_path, CONV)
    store.dir.mkdir(parents=True)
    store.index_path.write_text("{not json", encoding="utf-8")
    assert store.load().turns == []
    assert list(store.dir.glob("index.damaged-*.json"))


def test_the_logged_checkpoint_event_is_a_registered_type():
    from quickcode.session.wire import LOGGED_TYPES

    assert {FileCheckpointed.wire_type, "files_rewound"} <= LOGGED_TYPES
