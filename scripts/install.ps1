#Requires -Version 5.1
<#
.SYNOPSIS
    Local/dev installer for QuickCode - for people not using the Inno Setup
    installer (packaging/quickcode.iss). Installs QuickCode from this repo
    checkout into a virtual environment or via pipx.

.DESCRIPTION
    Usage (from the repo root):

        powershell -ExecutionPolicy Bypass -File scripts\install.ps1

    Or as a one-liner from anywhere:

        powershell -ExecutionPolicy Bypass -File "C:\path\to\QuickCode\scripts\install.ps1"

    By default this creates/reuses a virtual environment at .venv in the repo
    root and installs QuickCode into it. Pass -UsePipx to install with pipx
    instead (isolated, puts `quickcode` on PATH globally for the user).

    Written for Windows PowerShell 5.1: no &&, no ||, no ternary / ?? / ?.
    operators. All branching uses if/else and explicit $null checks.

.PARAMETER UsePipx
    Install with pipx instead of creating a local .venv.

.PARAMETER Dev
    Install the "dev" extras (pytest, ruff, etc.) alongside QuickCode.
    Only applies to the .venv path.

.PARAMETER SkipDependencyCheck
    Skip the Git/Python auto-install step (assumes both are already present).
#>

[CmdletBinding()]
param(
    [switch]$UsePipx,
    [switch]$Dev,
    [switch]$SkipDependencyCheck
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Ok {
    param([string]$Message)
    Write-Host "    [OK] $Message" -ForegroundColor Green
}

function Write-Fail {
    param([string]$Message)
    Write-Host "    [FAIL] $Message" -ForegroundColor Red
}

function Test-CommandExists {
    param([string]$Name)
    $cmd = Get-Command -Name $Name -ErrorAction SilentlyContinue
    if ($null -eq $cmd) {
        return $false
    }
    return $true
}

# Refresh $env:Path for the current process from both Machine and User scope,
# so a tool installed by a child process (bootstrap.ps1) becomes visible here
# without needing a brand new shell.
function Update-SessionPath {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")

    if ($null -eq $machinePath) { $machinePath = "" }
    if ($null -eq $userPath) { $userPath = "" }

    if ($userPath -eq "") {
        $env:Path = $machinePath
    }
    else {
        $env:Path = $machinePath + ";" + $userPath
    }
}

# ---------------------------------------------------------------------------
# Resolve repo root (this script lives in <repo>\scripts\install.ps1)
# ---------------------------------------------------------------------------

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir

if (-not (Test-Path (Join-Path $repoRoot "pyproject.toml"))) {
    Write-Fail "Could not locate pyproject.toml next to scripts\install.ps1. Is the repo layout intact?"
    exit 1
}

Write-Host "QuickCode local installer" -ForegroundColor Magenta
Write-Host "Repo root: $repoRoot"

# ---------------------------------------------------------------------------
# Ensure Git / Python by calling bootstrap.ps1 as a child script in
# dependency-only mode (-SkipQuickCodeInstall). This reuses the exact same
# Git/Python detection-and-install logic without duplicating it or having to
# dot-source a script whose param block is otherwise mandatory/interactive.
# ---------------------------------------------------------------------------

if (-not $SkipDependencyCheck) {
    $bootstrapPath = Join-Path $scriptDir "bootstrap.ps1"
    if (-not (Test-Path $bootstrapPath)) {
        Write-Fail "bootstrap.ps1 not found at $bootstrapPath"
        exit 1
    }

    Write-Step "Ensuring Git and Python are available"

    & $bootstrapPath -SourceDir $repoRoot -SkipQuickCodeInstall
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Dependency check/install failed (bootstrap.ps1 exited with code $LASTEXITCODE)."
        exit 1
    }

    # Pick up anything bootstrap.ps1 just installed (git, python) before we
    # look for `python` on PATH below.
    Update-SessionPath
}

if (-not (Test-CommandExists -Name "python")) {
    Write-Fail "Python not found on PATH even after the dependency check. Install Python >= 3.12 manually and re-run, or re-run without -SkipDependencyCheck."
    exit 1
}

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

if ($UsePipx) {
    Write-Step "Installing QuickCode with pipx"

    if (-not (Test-CommandExists -Name "pipx")) {
        Write-Host "    pipx not found; installing it with pip --user first..." -ForegroundColor Yellow
        & python -m pip install --upgrade --user pipx
        & python -m pipx ensurepath
        Write-Host "    [WARN] pipx was just installed. You may need to open a NEW terminal for its PATH entry to take effect." -ForegroundColor Yellow
    }

    & pipx install --force "$repoRoot"
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "pipx install failed with exit code $LASTEXITCODE."
        exit 1
    }

    Write-Ok "QuickCode installed via pipx."
}
else {
    Write-Step "Creating / reusing virtual environment at .venv"

    $venvPath = Join-Path $repoRoot ".venv"
    $venvPython = Join-Path $venvPath "Scripts\python.exe"

    if (-not (Test-Path $venvPython)) {
        & python -m venv $venvPath
        if ($LASTEXITCODE -ne 0) {
            Write-Fail "Failed to create virtual environment."
            exit 1
        }
        Write-Ok "Virtual environment created at $venvPath"
    }
    else {
        Write-Ok "Reusing existing virtual environment at $venvPath"
    }

    Write-Step "Installing QuickCode into the virtual environment"

    $extras = ""
    if ($Dev) {
        $extras = "[dev,pty]"
    }
    else {
        $extras = "[pty]"
    }
    $installSpec = "$repoRoot$extras"

    & $venvPython -m pip install --upgrade pip
    & $venvPython -m pip install --editable $installSpec
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "pip install failed with exit code $LASTEXITCODE."
        exit 1
    }

    Write-Ok "QuickCode installed into $venvPath"
    Write-Host ""
    Write-Host "    Run it with:" -ForegroundColor Cyan
    Write-Host "        $venvPath\Scripts\quickcode.exe"
    Write-Host "    or activate the venv first:" -ForegroundColor Cyan
    Write-Host "        $venvPath\Scripts\Activate.ps1"
    Write-Host "        quickcode"
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host " QuickCode install complete." -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
exit 0
