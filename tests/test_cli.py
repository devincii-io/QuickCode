"""CLI argument shapes: `qc [path] [prompt]` disambiguation."""

from __future__ import annotations

import pytest

from quickcode.cli import _build_parser, _resolve_positionals


def resolve(argv: list[str]):
    args = _build_parser().parse_args(argv)
    _resolve_positionals(args)
    return args


def test_no_positionals(tmp_path):
    args = resolve([])
    assert args.prompt is None
    assert args.cwd is None
    assert args.project_given is False


def test_directory_positional_is_the_project(tmp_path):
    args = resolve([str(tmp_path)])
    assert args.cwd == str(tmp_path)
    assert args.prompt is None
    assert args.project_given is True


def test_non_directory_positional_is_the_prompt():
    args = resolve(["fix the build"])
    assert args.prompt == "fix the build"
    assert args.cwd is None
    assert args.project_given is False


def test_directory_then_prompt(tmp_path):
    args = resolve([str(tmp_path), "fix the build"])
    assert args.cwd == str(tmp_path)
    assert args.prompt == "fix the build"


def test_explicit_cwd_wins_over_the_positional(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    args = resolve(["--cwd", str(other), str(tmp_path), "go"])
    assert args.cwd == str(other)
    assert args.prompt == "go"


def test_two_non_directory_positionals_are_rejected():
    with pytest.raises(SystemExit) as exc:
        resolve(["hello", "world"])
    assert exc.value.code == 2


def test_the_cli_and_the_server_report_the_packages_one_version(tmp_path, monkeypatch, capsys):
    """``quickcode.__version__`` is the only source: the CLI used to resolve its
    own copy at import, with a different fallback for a source checkout."""
    import importlib.metadata

    from quickcode.cli import main
    from tests.test_server import FakeProvider, make_client, make_manager

    def not_installed(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", not_installed)

    main(["--version"])
    assert capsys.readouterr().out.strip() == "quickcode 0.0.0-dev"
    with make_client(make_manager(tmp_path, FakeProvider([]))) as client:
        assert client.get("/api/health").json()["version"] == "0.0.0-dev"
        assert client.get("/api/bootstrap").json()["version"] == "0.0.0-dev"
