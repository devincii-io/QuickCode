"""Local release gate and release-artifact builder for QuickCode.

Mirrors QuickTerm's ``scripts/check.py`` + manual release workflow
(``AGENTS.md`` there), adapted to how QuickCode actually ships: there is no
PyInstaller/frozen build, so a "release" is a wheel + sdist (``uv build``)
plus a Windows installer (``packaging/quickcode.iss``, Inno Setup) that
pip-installs the wheel into a private venv at install time. Both consume the
one version that matters at runtime -- ``pyproject.toml``'s
``[project].version`` -- so this script is also the only place that bumps
it, to keep the installer, the wheel, and ``uv.lock`` from drifting apart.

Usage (from the repo root, PowerShell):

    .venv\\Scripts\\python.exe scripts\\release.py --check
        # pytest + ruff + byte-compile + JS syntax check + clean-diff check.
        # What CONTRIBUTING.md asks you to run before a PR.

    .venv\\Scripts\\python.exe scripts\\release.py --version 2.0.0
        # Bump pyproject.toml's version, re-lock (`uv lock`), run --check,
        # `uv build` a wheel + sdist, compile the Inno Setup installer with
        # /DMyAppVersion=2.0.0, and write SHA256SUMS.txt over all three.
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
DIST = ROOT / "dist"  # uv build's wheel + sdist land here.
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

    Deliberately does NOT touch ``quickcode/__init__.py``'s ``__version__``:
    that value is unused dead code (``quickcode/cli.py`` reads the version
    via ``importlib.metadata.version("quickcode")`` instead), not a second
    source of truth QuickTerm-style. See AGENTS.md.
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


def check() -> None:
    version = project_version()
    print(f"QuickCode {version} local release gate", flush=True)
    run(sys.executable, "-m", "pytest", "-q")
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


def build(iscc_path: str | None) -> None:
    version = project_version()
    print(f"Building QuickCode {version} release artifacts", flush=True)

    # Wheel + sdist.
    run("uv", "build", "--out-dir", "dist")

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
