"""The circuit breakers read the command, not one spelling of it.

They were regexes, and each knew one shape: flags directly after `rm`, the
target directly after the flags, `push` directly after `git`, a fork bomb
whose function is called `:`. In yolo the breakers are all that stops, and
every spelling below ran there without a prompt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quickcode.core.permissions import Decision, Mode, PermissionEngine, Rules


def yolo(root: Path) -> PermissionEngine:
    return PermissionEngine(mode=Mode.yolo, rules=Rules(), root=root)


@pytest.mark.parametrize("command", [
    "rm -rf --no-preserve-root /", "rm -rf -- /", "rm -rf build /", "rm -r -f -v /",
    "rm -rf ~/", "rm -rf ~/*", "rm -rf $HOME", 'rm -rf "$HOME"', "rm -rf ${HOME:?}/",
    "rm -rf //", "rm -rf /.", "rm -rf /usr", "rm -rf /home", "rm -rf ~root",
    f"rm -rf {Path.home()}", "sudo rm -rf /", "xargs rm -rf /", "bash -c 'rm -rf /'",
    "echo $(rm -rf /)", "Remove-Item -Recurse -Force C:\\", "rm -r -fo $env:USERPROFILE",
    "rd /s /q C:\\", "rmdir /s /q %USERPROFILE%", "rm -rf /c/", "rm -rf C:/Windows",
])
def test_deleting_the_root_or_a_home_directory_stops_however_it_is_spelled(command, tmp_path):
    assert yolo(tmp_path).evaluate("bash", command) == Decision.ask


@pytest.mark.parametrize("command", [
    "git -C . push -f", "git push -uf origin main", "git push -fu origin main",
    "git push origin +main", "git push origin +HEAD:main", "git push --mirror",
    "git push --force-with-lease=main", "git push --force-if-includes --force-with-lease",
    "git -c alias.p='push --force' p", "git -c alias.p='!git push -f' p",
    "git -c remote.origin.push=+refs/heads/*:refs/heads/* push",
    "git --no-pager push origin main --force", "cd repo && git push -f",
])
def test_a_forced_push_stops_however_it_is_spelled(command, tmp_path):
    assert yolo(tmp_path).evaluate("bash", command) == Decision.ask


@pytest.mark.parametrize("command", [
    ":(){ :|:& };:", ":(){ :|: & };:", "bomb(){ bomb|bomb& };bomb",
    "f() { f | f & }; f", "function b { b|b& }; b", "perl -e 'fork while fork'",
])
def test_a_fork_bomb_stops_whatever_its_function_is_called(command, tmp_path):
    assert yolo(tmp_path).evaluate("bash", command) == Decision.ask


@pytest.mark.parametrize("command", [
    "rm -rf build", "rm -rf ./dist /tmp/cache", "rm -rf ~/project/build",
    'rm -rf "$HOME/proj/build"', "rm -f /tmp/x.lock", "rm -rf node_modules",
    "git push origin main", "git push -u origin main", "git push --follow-tags",
    "git push -o ci.skip origin main", 'git commit -m "remove the rm -rf / guard"',
    'echo "git push --force"', "grep -rf pat.txt src", "f() { f; }", "ls -la /",
])
def test_the_breakers_still_do_not_cry_wolf(command, tmp_path):
    assert yolo(tmp_path).evaluate("bash", command) == Decision.allow
