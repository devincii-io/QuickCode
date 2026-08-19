"""Local release gate and release-artifact builder for QuickCode.

Mirrors QuickTerm's ``scripts/check.py`` + manual release workflow
(``AGENTS.md`` there). A QuickCode release is three artifacts:

* a **frozen application folder** (``quickcode.spec``, PyInstaller onedir),
  wrapped by the Inno Setup installer (``packaging/quickcode.iss``). This is
  the turnkey path: it needs neither Python nor Git on the user's machine.
* a **wheel** and an **sdist** (``uv build``), so ``pip``/``uv`` installs stay
  supported for people who already have Python.

All of them consume the one version that matters at runtime --
``pyproject.toml``'s ``[project].version`` -- so this script is also the only
place that bumps it, to keep the installer, the wheel, and ``uv.lock`` from
drifting apart.

Usage (from the repo root, PowerShell):

    .venv\\Scripts\\python.exe scripts\\release.py --check
        # pytest + ruff + byte-compile + JS syntax check + clean-diff check.
        # What CONTRIBUTING.md asks you to run before a PR.

    .venv\\Scripts\\python.exe scripts\\release.py --version 2.0.0
        # Bump pyproject.toml's version, re-lock (`uv lock`), run --check,
        # `uv build` a wheel + sdist, freeze the app with PyInstaller, compile
        # the Inno Setup installer with /DMyAppVersion=2.0.0 around it, and
        # write SHA256SUMS.txt over all three artifacts.
        # Does NOT tag, commit, or push -- that is a deliberate manual step.

    .venv\\Scripts\\python.exe scripts\\release.py --build
        # Same build + checksum step, without touching the version.

    .venv\\Scripts\\python.exe scripts\\release.py --verify-artifacts
        # Re-verify dist/ against SHA256SUMS.txt (e.g. right before
        # `gh release create`).
"""

from __future__ import annotations

import argparse
import compileall
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
ISS = ROOT / "packaging" / "quickcode.iss"
SPEC = ROOT / "quickcode.spec"
DIST = ROOT / "dist"  # uv build's wheel + sdist land here.
# PyInstaller's COLLECT output, named by quickcode.spec, and what the .iss
# copies into the install directory. Both executables plus _internal\.
FROZEN_DIR = DIST / "QuickCode"
FROZEN_EXES = ("quickcode.exe", "QuickCodeApp.exe")
# packaging/quickcode.iss has `OutputDir=dist`, resolved relative to the
# .iss file's own directory -- so the installer lands in packaging/dist/,
# not the repo-root dist/ next to the wheel. Both are covered by the
# repo-wide `dist/` gitignore rule.
INSTALLER_DIST = ROOT / "packaging" / "dist"

# Candidate locations for the Inno Setup compiler. A user-scope install
# under %LOCALAPPDATA% is the common case (no admin rights needed); the two
# Program Files paths cover a machine-wide install.
ISCC_CANDIDATES = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
    Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
    Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
]


def run(*args: str, cwd: Path = ROOT) -> None:
    print(f"+ {' '.join(args)}", flush=True)
    subprocess.run(args, cwd=cwd, check=True)


def project_version() -> str:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return data["project"]["version"]


def set_version(new_version: str) -> None:
    """Rewrite ``[project].version`` in pyproject.toml in place.

    There is nothing else to touch: ``quickcode/__init__.py`` resolves
    ``__version__`` out of the installed distribution's metadata rather than
    holding a literal, so pyproject.toml is the only place the number lives.
    The frozen build carries that metadata (``copy_metadata`` in
    quickcode.spec), which is why ``build()`` insists the environment is
    in sync before freezing. See AGENTS.md.
    """
    text = PYPROJECT.read_text(encoding="utf-8")
    new_text, n = re.subn(
        r'(?m)^version = "[^"]+"$', f'version = "{new_version}"', text, count=1
    )
    if n != 1:
        raise RuntimeError("could not find a single top-level `version = \"...\"` line to replace")
    PYPROJECT.write_text(new_text, encoding="utf-8")
    print(f"pyproject.toml: version -> {new_version}")


def check_javascript() -> None:
    """Syntax-check every ES module. No JS test suite exists yet (unlike
    QuickTerm's Node test runner) -- add one under tests/js/ and wire it in
    here if the frontend grows logic worth unit testing directly."""
    node = shutil.which("node")
    if node is None:
        print("node not found on PATH; skipping JavaScript syntax check", flush=True)
        return
    js_files = sorted((ROOT / "quickcode" / "frontend" / "js").rglob("*.js"))
    if not js_files:
        raise RuntimeError("no frontend JavaScript files found")
    for path in js_files:
        run(node, "--check", str(path))


# pytest-timeout's banner. Seeing it means a test was killed on the clock, and
# in this suite that has one known cause: creating an asyncio event loop on
# Windows builds its self-pipe with ``socket.socketpair()``, which binds a
# loopback listener and calls ``accept()`` with no timeout -- and that call
# sometimes never returns. Confirmed with live stack dumps under both
# starlette's TestClient portal and pytest-asyncio's runner. It is not our code,
# the selector loop uses the same self-pipe, and it strikes a different test
# every time.
_TIMEOUT_BANNER = "+++ Timeout +++"
PYTEST_ATTEMPTS = 3


def run_tests() -> None:
    """The suite, retried *only* when the environment hang above killed it.

    A retry loop over a test suite is normally how a real failure gets ignored,
    so this one refuses to be that: anything other than the timeout banner fails
    on the first attempt, and a run that only ever times out fails too, loudly,
    rather than being reported as a pass.
    """
    for attempt in range(1, PYTEST_ATTEMPTS + 1):
        print(f"+ {sys.executable} -m pytest -q  (attempt {attempt})", flush=True)
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=ROOT, capture_output=True, text=True,
        )
        print(proc.stdout, end="", flush=True)
        if proc.returncode == 0:
            return
        if _TIMEOUT_BANNER not in (proc.stdout + proc.stderr):
            print(proc.stderr, end="", file=sys.stderr, flush=True)
            raise RuntimeError("pytest failed")
        print(
            f"pytest was killed by the socketpair hang (attempt {attempt} of "
            f"{PYTEST_ATTEMPTS}); no test reported a failure, retrying.",
            flush=True,
        )
    raise RuntimeError(
        f"pytest hit the socketpair hang on all {PYTEST_ATTEMPTS} attempts; the "
        "suite never completed, so nothing was verified. Try again on a quieter "
        "machine before releasing."
    )


def check() -> None:
    version = project_version()
    print(f"QuickCode {version} local release gate", flush=True)
    run_tests()
    run(sys.executable, "-m", "ruff", "check", "quickcode", "tests", "scripts")
    if not compileall.compile_dir(ROOT / "quickcode", quiet=1):
        raise RuntimeError("Python byte compilation failed")
    check_javascript()
    run("git", "diff", "--check")
    print("Local release gate passed.", flush=True)


def find_iscc() -> Path:
    for candidate in ISCC_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise RuntimeError(
        "ISCC.exe (Inno Setup 6 Compiler) not found in any known location: "
        f"{[str(c) for c in ISCC_CANDIDATES]}. Install Inno Setup 6+ or pass "
        "its path via --iscc."
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def freeze() -> None:
    """PyInstaller the onedir app into dist/QuickCode.

    Run from this interpreter rather than a bare ``pyinstaller`` on PATH: the
    frozen build inherits the *environment it is built in*, so a stray global
    PyInstaller pointed at some other Python would quietly ship a different
    runtime and a different dependency set than the one the tests just passed
    against.
    """
    # quickcode.spec bundles this environment's dist-info so the frozen build
    # can answer importlib.metadata.version("quickcode") -- which is what
    # --version, /api/health and the update check all read. A stale editable
    # install would ship yesterday's number and make the updater offer an
    # update to the version already running.
    from importlib.metadata import version as installed_version

    declared = project_version()
    installed = installed_version("quickcode")
    if installed != declared:
        raise RuntimeError(
            f"this environment has quickcode {installed} installed but "
            f"pyproject.toml declares {declared}; the frozen build would report "
            "the wrong version. Run `uv sync` first."
        )
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(SPEC))
    missing = [name for name in FROZEN_EXES if not (FROZEN_DIR / name).is_file()]
    if missing:
        # Notably how a case-insensitive name collision between the two
        # executables would present: the build succeeds, one of them is gone.
        raise RuntimeError(f"PyInstaller did not produce: {missing}")


def build(iscc_path: str | None) -> None:
    version = project_version()
    print(f"Building QuickCode {version} release artifacts", flush=True)

    # Wheel + sdist. Still built, and still supported: `pip install` is a
    # first-class way to run QuickCode for anyone who already has Python.
    run("uv", "build", "--out-dir", "dist")

    # The frozen app the installer wraps. Before ISCC, which copies its output.
    freeze()

    # Windows installer, version injected so it can never disagree with
    # pyproject.toml (packaging/quickcode.iss falls back to its own literal
    # only when compiled directly from the Inno Setup IDE).
    iscc = Path(iscc_path) if iscc_path else find_iscc()
    run(str(iscc), "/Q", f"/DMyAppVersion={version}", "packaging\\quickcode.iss")

    artifacts = [
        INSTALLER_DIST / f"QuickCode-Setup-{version}.exe",
        DIST / f"quickcode-{version}-py3-none-any.whl",
        DIST / f"quickcode-{version}.tar.gz",
    ]
    missing = [str(p.relative_to(ROOT)) for p in artifacts if not p.is_file()]
    if missing:
        raise RuntimeError(f"expected release artifacts missing after build: {missing}")

    sums_path = ROOT / "SHA256SUMS.txt"
    lines = [f"{sha256(p)}  {p.name}" for p in artifacts]
    sums_path.write_text("\n".join(lines) + "\n", encoding="ascii")
    print(f"Wrote {sums_path.relative_to(ROOT)}:")
    for line in lines:
        print(f"  {line}")


def verify_artifacts() -> None:
    version = project_version()
    artifacts = [
        INSTALLER_DIST / f"QuickCode-Setup-{version}.exe",
        DIST / f"quickcode-{version}-py3-none-any.whl",
        DIST / f"quickcode-{version}.tar.gz",
    ]
    missing = [str(p.relative_to(ROOT)) for p in artifacts if not p.is_file()]
    if missing:
        raise RuntimeError(f"missing release artifacts: {', '.join(missing)}")

    sums_path = ROOT / "SHA256SUMS.txt"
    expected = {
        line.split()[1]: line.split()[0].lower()
        for line in sums_path.read_text(encoding="ascii").splitlines()
        if line.strip()
    }
    actual_names = {p.name for p in artifacts}
    if set(expected) != actual_names:
        raise RuntimeError(
            f"checksum manifest names differ: expected={sorted(actual_names)}, "
            f"found={sorted(expected)}"
        )
    for path in artifacts:
        if sha256(path) != expected[path.name]:
            raise RuntimeError(f"checksum mismatch: {path.name}")
    print(f"All {len(artifacts)} release artifacts match SHA256SUMS.txt.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", metavar="X.Y.Z", help="bump pyproject.toml, re-lock, check, and build")
    parser.add_argument("--check", action="store_true", help="run the local release gate only")
    parser.add_argument("--build", action="store_true", help="build wheel/sdist + installer, write SHA256SUMS.txt")
    parser.add_argument("--verify-artifacts", action="store_true", help="verify dist/ against SHA256SUMS.txt")
    parser.add_argument("--iscc", metavar="PATH", help="explicit path to ISCC.exe (Inno Setup compiler)")
    args = parser.parse_args()

    if not any([args.version, args.check, args.build, args.verify_artifacts]):
        parser.error("pass one of --version, --check, --build, --verify-artifacts")

    if args.version:
        set_version(args.version)
        run("uv", "lock")
        # Re-install the project so its dist-info carries the new number: the
        # frozen build copies that metadata, and `uv lock` alone does not
        # touch the environment.
        run("uv", "sync")
        check()
        build(args.iscc)
    else:
        if args.check:
            check()
        if args.build:
            build(args.iscc)
        if args.verify_artifacts:
            verify_artifacts()

    print(
        "\nNot done automatically (by design -- see AGENTS.md): "
        "`git add`, review the diff, commit, `git tag v<version>`, "
        "`gh release create v<version> dist/* SHA256SUMS.txt`, push.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
