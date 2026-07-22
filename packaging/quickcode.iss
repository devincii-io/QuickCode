; ============================================================================
; QuickCode - Inno Setup installer script
;
; Builds a Windows installer that:
;   - Copies the QuickCode source tree + bundled scripts into the install dir
;   - Runs scripts\bootstrap.ps1 post-install to ensure Git + Python (>=3.12)
;     are present and to `pip install` QuickCode
;   - Optionally adds QuickCode's install dir to the user PATH
;   - Creates a Start Menu shortcut that opens a terminal running `quickcode`
;
; Build with the Inno Setup Compiler (ISCC.exe):
;   ISCC.exe packaging\quickcode.iss
;
; See packaging\README.md for full build instructions.
; ============================================================================

#define MyAppName "QuickCode"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "Fichtel Systems"
#define MyAppURL "https://fichtelsystems.de"
#define MyAppContact "kontakt@fichtelsystems.de"

; RepoRoot is the QuickCode repository checkout this .iss lives in
; (packaging\quickcode.iss -> repo root is one level up).
#define RepoRoot ".."

[Setup]
AppId={{6C8E6F2D-6E6A-4E6B-9C5A-2F5B1B8C6D3E}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL=mailto:{#MyAppContact}
AppUpdatesURL={#MyAppURL}
AppContact={#MyAppContact}

; Per-user install by default; the "dialog" override lets a user elevate to
; an all-users install from the wizard's privilege prompt if they want to.
DefaultDirName={autopf}\{#MyAppName}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes

LicenseFile={#RepoRoot}\LICENSE
OutputDir=Output
OutputBaseFilename=QuickCode-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes

; Modern wizard chrome (Inno Setup 6.1+).
WizardStyle=modern

; We modify PATH (a machine/user environment variable), so Explorer and other
; processes need to be told the environment changed.
ChangesEnvironment=yes

; No architecture-specific binaries are shipped; QuickCode is pure Python.
ArchitecturesInstallIn64BitMode=x64compatible

; No standalone .exe is shipped (QuickCode is installed via pip into the
; user's Python environment), so we let Inno Setup use its own default icons
; for both the wizard and the uninstall entry.

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "addtopath"; Description: "Add QuickCode to my PATH (recommended)"; GroupDescription: "Additional tasks:"; Flags: checkedonce

[Files]
; Bundle the full QuickCode source tree (everything pip needs to build/install
; the package) plus the packaging scripts, excluding dev/build cruft.
Source: "{#RepoRoot}\quickcode\*"; DestDir: "{app}\src\quickcode"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#RepoRoot}\pyproject.toml"; DestDir: "{app}\src"; Flags: ignoreversion
Source: "{#RepoRoot}\README.md"; DestDir: "{app}\src"; Flags: ignoreversion
Source: "{#RepoRoot}\LICENSE"; DestDir: "{app}\src"; Flags: ignoreversion
Source: "{#RepoRoot}\scripts\bootstrap.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "{#RepoRoot}\scripts\install.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion

[Icons]
; Start Menu entry that opens a terminal already running `quickcode`.
Name: "{group}\{#MyAppName}"; Filename: "{cmd}"; Parameters: "/K ""quickcode"""; WorkingDir: "%USERPROFILE%"; IconFilename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Comment: "Launch QuickCode"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"

[Run]
; Post-install: run the bootstrap script to ensure Git/Python and pip-install
; QuickCode from the bundled source directory. Runs visibly (not hidden) so
; the user can see progress and any prompts (e.g. UAC for winget/installers).
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; \
    Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\scripts\bootstrap.ps1"" -SourceDir ""{app}\src"""; \
    WorkingDir: "{app}"; \
    StatusMsg: "Setting up Git, Python and QuickCode (this can take a few minutes)..."; \
    Flags: runascurrentuser waituntilterminated

[Code]
{ ---------------------------------------------------------------------------
  PATH handling: append/remove the user's Python "Scripts" install location
  and the QuickCode source dir so `quickcode` resolves after bootstrap.ps1
  installs the package with `pip install --user`.
  Inno Setup does not know the exact Scripts dir bootstrap.ps1 ends up using
  (it depends on which Python got installed/found), so instead we add a
  small, stable set of well-known per-user locations that cover the winget
  and python.org installer layouts, plus the actual user PATH entries that
  bootstrap.ps1/pip itself will have already appended (Update-SessionPath
  logic mirrors this). Adding the same dir twice is harmless; ExpandPath
  helpers below de-duplicate what they can.
  --------------------------------------------------------------------------- }

const
  EnvironmentKey = 'Environment';

function GetUserPythonScriptsGuess(): String;
var
  PyVerDir: String;
begin
  { Typical per-user pip --user install location on Windows for CPython 3.12:
    %APPDATA%\Python\Python312\Scripts }
  PyVerDir := ExpandConstant('{userappdata}') + '\Python\Python312\Scripts';
  Result := PyVerDir;
end;

procedure EnvAddPath(Path: string);
var
  Paths: string;
begin
  { Read the current user PATH, skip if already present, else append. }
  if not RegQueryStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', Paths) then
    Paths := '';

  if Paths = '' then
  begin
    RegWriteStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', Path);
    Exit;
  end;

  if Pos(';' + Uppercase(Path) + ';', ';' + Uppercase(Paths) + ';') > 0 then
    Exit; { already present }

  if Paths[Length(Paths)] = ';' then
    RegWriteStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', Paths + Path)
  else
    RegWriteStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', Paths + ';' + Path);
end;

procedure EnvRemovePath(Path: string);
var
  Paths: string;
  P: Integer;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', Paths) then
    Exit;

  P := Pos(';' + Uppercase(Path) + ';', ';' + Uppercase(Paths) + ';');
  if P = 0 then
    Exit;

  { Rebuild the string without the target entry, preserving neighbours. }
  Paths := ';' + Paths + ';';
  StringChangeEx(Paths, ';' + Path + ';', ';', True);
  { Trim leading/trailing separators added above. }
  if (Length(Paths) > 0) and (Paths[1] = ';') then
    Delete(Paths, 1, 1);
  if (Length(Paths) > 0) and (Paths[Length(Paths)] = ';') then
    Delete(Paths, Length(Paths), 1);

  RegWriteStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', Paths);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    if IsTaskSelected('addtopath') then
    begin
      EnvAddPath(ExpandConstant('{app}\scripts'));
      EnvAddPath(GetUserPythonScriptsGuess());
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    EnvRemovePath(ExpandConstant('{app}\scripts'));
    EnvRemovePath(GetUserPythonScriptsGuess());
  end;
end;
