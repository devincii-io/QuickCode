#Requires -Version 5.1
<#
.SYNOPSIS
    QuickCode post-install bootstrap: ensures Git and Python (>=3.12) are present,
    then installs the QuickCode package from a bundled source directory.

.DESCRIPTION
    Run by the Inno Setup installer (packaging/quickcode.iss) after files are copied,
    but it is also safe to run standalone, e.g.:

        powershell -ExecutionPolicy Bypass -File bootstrap.ps1 -SourceDir "C:\path\to\quickcode"

    Written for Windows PowerShell 5.1: no &&, no ||, no ternary / ?? / ?. operators.
    All branching uses if/else and explicit $null checks.

.PARAMETER SourceDir
    Path to the QuickCode source tree (containing pyproject.toml) to pip-install.

.PARAMETER Quiet
    If set, suppresses winget/installer UI where possible (best-effort; some
    installers always show a UAC prompt).

.PARAMETER SkipQuickCodeInstall
    If set, only ensures Git and Python are present and exits - does not
    pip-install QuickCode itself. Used by scripts\install.ps1, which handles
    the actual package install itself (venv or pipx) after dependencies are
    confirmed.

.EXITCODE
    0 on success. Non-zero on hard failure (Git/Python could not be made available,
    or the QuickCode install itself failed).
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SourceDir,

    [switch]$Quiet,

    [switch]$SkipQuickCodeInstall
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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

function Write-Warn2 {
    param([string]$Message)
    Write-Host "    [WARN] $Message" -ForegroundColor Yellow
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

function Test-WingetAvailable {
    return Test-CommandExists -Name "winget"
}

# Refresh $env:Path for the current process from both Machine and User scope,
# so a just-installed tool becomes visible without spawning a new shell.
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
# Git
# ---------------------------------------------------------------------------

function Install-GitViaWinget {
    Write-Info "Installing Git via winget..."
    $wingetArgs = @(
        "install", "--id", "Git.Git", "-e",
        "--accept-source-agreements", "--accept-package-agreements",
        "--silent"
    )
    $proc = Start-Process -FilePath "winget" -ArgumentList $wingetArgs -Wait -PassThru -NoNewWindow
    if ($proc.ExitCode -eq 0) {
        return $true
    }
    Write-Warn2 "winget exited with code $($proc.ExitCode) while installing Git."
    return $false
}

function Install-GitViaDirectDownload {
    Write-Info "Downloading Git for Windows installer..."

    # Known-good redirect that always points at the latest 64-bit "Git for Windows" installer.
    $downloadUrl = "https://github.com/git-for-windows/git/releases/latest/download/Git-64-bit.exe"
    $installerPath = Join-Path $env:TEMP "QuickCode-GitInstaller.exe"

    try {
        Invoke-WebRequest -Uri $downloadUrl -OutFile $installerPath -UseBasicParsing
    }
    catch {
        Write-Warn2 "Could not download Git installer directly: $($_.Exception.Message)"
        return $false
    }

    if (-not (Test-Path $installerPath)) {
        Write-Warn2 "Git installer download did not produce a file."
        return $false
    }

    Write-Info "Running Git installer silently..."
    $proc = Start-Process -FilePath $installerPath -ArgumentList @("/VERYSILENT", "/NORESTART", "/NOCANCEL", "/SP-") -Wait -PassThru
    Remove-Item -Path $installerPath -Force -ErrorAction SilentlyContinue

    if ($proc.ExitCode -eq 0) {
        return $true
    }
    Write-Warn2 "Git installer exited with code $($proc.ExitCode)."
    return $false
}

function Ensure-Git {
    Write-Step "Checking for Git"

    if (Test-CommandExists -Name "git") {
        $gitVersion = git --version
        Write-Ok "Git already present: $gitVersion"
        return $true
    }

    Write-Info "Git not found on PATH. Attempting to install it."

    $installed = $false
    if (Test-WingetAvailable) {
        $installed = Install-GitViaWinget
    }
    else {
        Write-Warn2 "winget is not available on this system."
    }

    if (-not $installed) {
        $installed = Install-GitViaDirectDownload
    }

    Update-SessionPath

    if (Test-CommandExists -Name "git") {
        $gitVersion = git --version
        Write-Ok "Git installed: $gitVersion"
        return $true
    }

    Write-Fail "Git could not be installed automatically."
    return $false
}

# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------

# Returns $true if the given `python`-style command resolves to Python >= 3.12.
function Test-PythonVersionOk {
    param([string]$Command)

    if (-not (Test-CommandExists -Name $Command)) {
        return $false
    }

    try {
        $verOutput = & $Command -c "import sys; print('%d.%d' % (sys.version_info[0], sys.version_info[1]))" 2>$null
    }
    catch {
        return $false
    }

    if ($null -eq $verOutput -or $verOutput -eq "") {
        return $false
    }

    $parts = $verOutput.Trim().Split(".")
    if ($parts.Length -lt 2) {
        return $false
    }

    $major = [int]$parts[0]
    $minor = [int]$parts[1]

    if ($major -gt 3) {
        return $true
    }
    if ($major -eq 3 -and $minor -ge 12) {
        return $true
    }
    return $false
}

# Finds a usable Python command name ("python" or "py -3") already on PATH,
# or returns $null if none qualifies.
function Find-SuitablePython {
    if (Test-PythonVersionOk -Command "python") {
        return "python"
    }
    if (Test-CommandExists -Name "py") {
        # The Python launcher can select a specific version even if `python`
        # itself is shadowed by the Windows Store alias.
        try {
            $verOutput = & py -3 -c "import sys; print('%d.%d' % (sys.version_info[0], sys.version_info[1]))" 2>$null
        }
        catch {
            $verOutput = $null
        }
        if ($null -ne $verOutput -and $verOutput -ne "") {
            $parts = $verOutput.Trim().Split(".")
            if ($parts.Length -ge 2) {
                $major = [int]$parts[0]
                $minor = [int]$parts[1]
                if (($major -eq 3 -and $minor -ge 12) -or ($major -gt 3)) {
                    return "py -3"
                }
            }
        }
    }
    return $null
}

function Install-PythonViaWinget {
    Write-Info "Installing Python 3.12 via winget..."
    $wingetArgs = @(
        "install", "--id", "Python.Python.3.12", "-e",
        "--accept-source-agreements", "--accept-package-agreements",
        "--silent"
    )
    $proc = Start-Process -FilePath "winget" -ArgumentList $wingetArgs -Wait -PassThru -NoNewWindow
    if ($proc.ExitCode -eq 0) {
        return $true
    }
    Write-Warn2 "winget exited with code $($proc.ExitCode) while installing Python."
    return $false
}

function Install-PythonViaDirectDownload {
    Write-Info "Downloading python.org installer..."

    $downloadUrl = "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe"
    $installerPath = Join-Path $env:TEMP "QuickCode-PythonInstaller.exe"

    try {
        Invoke-WebRequest -Uri $downloadUrl -OutFile $installerPath -UseBasicParsing
    }
    catch {
        Write-Warn2 "Could not download Python installer directly: $($_.Exception.Message)"
        return $false
    }

    if (-not (Test-Path $installerPath)) {
        Write-Warn2 "Python installer download did not produce a file."
        return $false
    }

    Write-Info "Running Python installer silently (per-user, PATH prepended)..."
    $installerArgs = @("/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_launcher=1")
    $proc = Start-Process -FilePath $installerPath -ArgumentList $installerArgs -Wait -PassThru
    Remove-Item -Path $installerPath -Force -ErrorAction SilentlyContinue

    if ($proc.ExitCode -eq 0) {
        return $true
    }
    Write-Warn2 "Python installer exited with code $($proc.ExitCode)."
    return $false
}

function Ensure-Python {
    Write-Step "Checking for Python (>= 3.12)"

    $pythonCmd = Find-SuitablePython
    if ($null -ne $pythonCmd) {
        Write-Ok "Suitable Python already present ($pythonCmd)."
        return $pythonCmd
    }

    Write-Info "No suitable Python found on PATH. Attempting to install Python 3.12."

    $installed = $false
    if (Test-WingetAvailable) {
        $installed = Install-PythonViaWinget
    }
    else {
        Write-Warn2 "winget is not available on this system."
    }

    if (-not $installed) {
        $installed = Install-PythonViaDirectDownload
    }

    Update-SessionPath

    $pythonCmd = Find-SuitablePython
    if ($null -ne $pythonCmd) {
        Write-Ok "Python installed and detected ($pythonCmd)."
        return $pythonCmd
    }

    Write-Fail "Python >= 3.12 could not be installed automatically."
    return $null
}

# ---------------------------------------------------------------------------
# QuickCode install
# ---------------------------------------------------------------------------

function Install-QuickCode {
    param([string]$PythonCmd, [string]$Source)

    Write-Step "Installing QuickCode"

    if (-not (Test-Path $Source)) {
        Write-Fail "Source directory not found: $Source"
        return $false
    }

    $pyExe, $pyBaseArgs = Split-PythonCommand -PythonCmd $PythonCmd

    # Make sure pip itself is present and current; ensurepip is a no-op if it
    # already is, so this is safe to run every time (idempotent).
    Write-Info "Ensuring pip is available..."
    & $pyExe @pyBaseArgs -m ensurepip --upgrade *>$null

    $havePipx = Test-CommandExists -Name "pipx"

    if ($havePipx) {
        Write-Info "pipx detected; installing QuickCode with pipx (isolated environment)."
        # --force makes re-runs idempotent (upgrades in place instead of erroring).
        & pipx install --force "$Source"
        $exitCode = $LASTEXITCODE
        if ($exitCode -eq 0) {
            Write-Ok "QuickCode installed via pipx."
            return $true
        }
        Write-Warn2 "pipx install failed (exit $exitCode); falling back to pip."
    }

    Write-Info "Installing QuickCode with pip (--user)..."
    & $pyExe @pyBaseArgs -m pip install --upgrade --user "$Source"
    $exitCode = $LASTEXITCODE

    if ($exitCode -ne 0) {
        Write-Fail "pip install failed with exit code $exitCode."
        return $false
    }

    Write-Ok "QuickCode installed via pip."
    return $true
}

# Splits a python command string like "py -3" into an executable + argument
# array, since PowerShell 5.1 can't `& "py -3"` as a single token.
function Split-PythonCommand {
    param([string]$PythonCmd)

    $tokens = $PythonCmd.Split(" ")
    $exe = $tokens[0]
    $rest = @()
    if ($tokens.Length -gt 1) {
        $rest = $tokens[1..($tokens.Length - 1)]
    }
    return $exe, $rest
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

function Main {
    Write-Host "QuickCode bootstrap starting..." -ForegroundColor Magenta
    Write-Info "Source directory: $SourceDir"

    $gitOk = Ensure-Git
    if (-not $gitOk) {
        Write-Warn2 "Continuing without a confirmed Git install. QuickCode itself does not require Git at runtime, but some workflows (repo cloning, version control tools) will be unavailable until Git is installed."
    }

    $pythonCmd = Ensure-Python
    if ($null -eq $pythonCmd) {
        Write-Fail "Cannot proceed without Python >= 3.12."
        exit 1
    }

    if ($SkipQuickCodeInstall) {
        Write-Host ""
        Write-Host "Dependencies confirmed (Git + Python). Skipping QuickCode package install as requested." -ForegroundColor Green
        exit 0
    }

    $installOk = Install-QuickCode -PythonCmd $pythonCmd -Source $SourceDir
    if (-not $installOk) {
        Write-Fail "QuickCode installation failed."
        exit 1
    }

    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host " QuickCode is ready - open a new terminal and run: quickcode" -ForegroundColor Green
    Write-Host "============================================================" -ForegroundColor Green
    exit 0
}

Main
