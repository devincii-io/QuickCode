# QuickCode packaging

This directory holds the Windows installer for QuickCode. The companion
`scripts\` directory (repo root) holds the PowerShell scripts the installer
bundles and runs.

## Files

| File | Purpose |
|---|---|
| `packaging/quickcode.iss` | Inno Setup script that builds `QuickCode-Setup-<version>.exe` |
| `scripts/bootstrap.ps1` | Ensures Git + Python (>=3.12) are present, then `pip install`s QuickCode. Bundled into the installer and run automatically post-install. |
| `scripts/install.ps1` | Standalone local/dev installer for people who don't want the `.exe` (creates a `.venv` or uses `pipx`, calls `bootstrap.ps1` for the Git/Python check). |

## Option A: Build the Inno Setup installer

1. Install [Inno Setup](https://jrsoftware.org/isinfo.php) (6.x or later; the
   script uses `WizardStyle=modern`, available since 6.1).
2. From the repo root, compile the script with the Inno Setup Compiler:

   ```powershell
   & "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\quickcode.iss
   ```

   (Or open `packaging\quickcode.iss` in the Inno Setup IDE and press
   *Compile*.)
3. The compiled installer is written to `packaging\Output\QuickCode-Setup-<version>.exe`.

### What the installer does

- Installs per-user by default into `%LOCALAPPDATA%\Programs\QuickCode`
  (Inno's `{autopf}` constant), with an option in the elevation dialog to
  install for all users instead.
- Copies the QuickCode source tree (enough to `pip install` it) plus
  `scripts\bootstrap.ps1` / `scripts\install.ps1` into the install directory.
- Runs `bootstrap.ps1` automatically after files are copied, which:
  - Installs Git if it's missing (via `winget`, falling back to the official
    Git for Windows installer, run silently).
  - Installs Python 3.12 if no suitable Python (>=3.12) is on `PATH` (via
    `winget`, falling back to the official python.org installer, run
    silently, per-user).
  - `pip install`s QuickCode from the bundled source directory.
- Offers a checked-by-default task to add QuickCode to the user `PATH`
  (the install's `scripts` dir plus the expected per-user pip `Scripts`
  directory), and cleans that up again on uninstall.
- Adds a Start Menu shortcut ("QuickCode") that opens a terminal and runs
  `quickcode` directly.

Because the installer changes `PATH`, `ChangesEnvironment=yes` is set so
Explorer/new processes pick up the change without a reboot (a new terminal
window is still required for the shell that's about to run `quickcode`).

## Option B: Local / dev install via PowerShell

If you already have this repo checked out and just want QuickCode installed
without building an installer:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

By default this creates (or reuses) a `.venv` in the repo root and installs
QuickCode into it in editable mode. Useful flags:

- `-UsePipx` — install with [pipx](https://pypa.github.io/pipx/) instead,
  which puts `quickcode` on your user `PATH` globally rather than inside a
  project-local venv.
- `-Dev` — also install the `dev` extras (`pytest`, `ruff`, ...). Only
  applies to the `.venv` path.
- `-SkipDependencyCheck` — skip the Git/Python auto-install step (use when
  you already know both are present and want a faster run).

`install.ps1` calls `bootstrap.ps1` under the hood (in dependency-only mode,
`-SkipQuickCodeInstall`) to reuse the exact same Git/Python detection and
silent-install logic described above, then does its own venv/pipx install.

## Notes

- Both scripts are written for **Windows PowerShell 5.1** (no `&&`/`||`
  chaining, no ternary/`??`/`?.` operators) since that's the default shell
  version shipped with Windows 11.
- Neither script requires an internet connection if Git and a suitable
  Python are already installed — the download/install paths are only
  exercised when something is actually missing.
- Nothing in this directory is executed as part of building/testing
  QuickCode itself; it's purely the distribution path for end users.
