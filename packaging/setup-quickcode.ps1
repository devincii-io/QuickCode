#Requires -Version 5.1
<#
.SYNOPSIS
    Post-install step for the QuickCode Inno Setup installer: ensure Git and
    Python (>=3.12), then install QuickCode into a private virtual environment
    inside the install directory.

.DESCRIPTION
    Run by packaging\quickcode.iss after the files are copied:

        powershell -NoProfile -ExecutionPolicy Bypass -File setup-quickcode.ps1 `
            -SourceDir "<app>\src" -VenvDir "<app>\venv"

    The Git/Python detection and silent-install logic is NOT duplicated here -
    it is reused by calling scripts\bootstrap.ps1 in dependency-only mode
    (-SkipQuickCodeInstall), the same way scripts\install.ps1 does.

    A dedicated venv under the install directory (rather than `pip install
    --user`) means QuickCode's dependencies can never collide with whatever
    else the user's Python has installed, the uninstaller can remove the whole
    tree, and the shortcut has a stable path to point at:

        <app>\venv\Scripts\quickcode-app.exe

    Written for Windows PowerShell 5.1: no &&, no ||, no ternary / ?? / ?.

.PARAMETER SourceDir
    Directory holding the bundled QuickCode source (pyproject.toml + package).

.PARAMETER VenvDir
    Directory the virtual environment is created in.

.PARAMETER SkipDependencyCheck
    Skip the Git/Python ensure step (assumes both are already present).
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SourceDir,

    [Parameter(Mandatory = $true)]
    [string]$VenvDir,

    [switch]$SkipDependencyCheck
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Info {
    param([string]$Message)
    Write-Host "    $Message"
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

# Refresh $env:Path from Machine + User scope so a Python that bootstrap.ps1
# just installed becomes visible without opening a new shell.
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

Write-Host "QuickCode setup" -ForegroundColor Magenta
Write-Info "Source: $SourceDir"
Write-Info "Venv:   $VenvDir"

if (-not (Test-Path (Join-Path $SourceDir "pyproject.toml"))) {
    Write-Fail "No pyproject.toml under $SourceDir - the bundled source is incomplete."
    exit 1
}

# ---------------------------------------------------------------------------
# Git + Python (>= 3.12), via the shared bootstrap script
# ---------------------------------------------------------------------------

if (-not $SkipDependencyCheck) {
    # bootstrap.ps1 is installed next to this script, in <app>\scripts.
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    $bootstrapPath = Join-Path $scriptDir "bootstrap.ps1"

    if (-not (Test-Path $bootstrapPath)) {
        Write-Fail "bootstrap.ps1 not found at $bootstrapPath"
        exit 1
    }

    Write-Step "Ensuring Git and Python (>= 3.12) are available"

    & $bootstrapPath -SourceDir $SourceDir -SkipQuickCodeInstall
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Dependency check failed (bootstrap.ps1 exited with code $LASTEXITCODE)."
        exit 1
    }

    Update-SessionPath
}

if (-not (Test-CommandExists -Name "python")) {
    Write-Fail "Python not found on PATH after the dependency check. Install Python >= 3.12 and re-run the installer."
    exit 1
}

# ---------------------------------------------------------------------------
# Virtual environment
# ---------------------------------------------------------------------------

$venvPython = Join-Path $VenvDir "Scripts\python.exe"

Write-Step "Creating the QuickCode virtual environment"

if (-not (Test-Path $venvPython)) {
    & python -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Failed to create the virtual environment at $VenvDir."
        exit 1
    }
    Write-Ok "Virtual environment created at $VenvDir"
}
else {
    Write-Ok "Reusing the existing virtual environment at $VenvDir"
}

# ---------------------------------------------------------------------------
# Install QuickCode (this downloads its dependencies from PyPI)
# ---------------------------------------------------------------------------

Write-Step "Installing QuickCode and its dependencies"

& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    Write-Fail "Could not upgrade pip inside the virtual environment."
    exit 1
}

# [pty] pulls in pywinpty for the interactive terminal tool on Windows.
$installSpec = "$SourceDir[pty]"
& $venvPython -m pip install --upgrade "$installSpec"
if ($LASTEXITCODE -ne 0) {
    Write-Fail "pip install failed with exit code $LASTEXITCODE."
    exit 1
}

# ---------------------------------------------------------------------------
# Verify the entry points the shortcuts and PATH rely on
# ---------------------------------------------------------------------------

$appExe = Join-Path $VenvDir "Scripts\quickcode-app.exe"
$cliExe = Join-Path $VenvDir "Scripts\quickcode.exe"

if (-not (Test-Path $appExe)) {
    Write-Fail "quickcode-app.exe was not created at $appExe - the Start Menu shortcut would not work."
    exit 1
}
if (-not (Test-Path $cliExe)) {
    Write-Fail "quickcode.exe was not created at $cliExe."
    exit 1
}

Write-Ok "QuickCode installed into $VenvDir"

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host " QuickCode is ready." -ForegroundColor Green
Write-Host " Start Menu: QuickCode   |   Terminal: quickcode  (or qc)" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
exit 0
