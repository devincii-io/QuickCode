; ============================================================================
; QuickCode - Inno Setup installer script
;
; Builds a per-user Windows installer that:
;   - Copies the QuickCode source tree + packaging scripts into the install dir
;   - Ensures Git and Python (>= 3.12) are present, then creates a private
;     virtual environment under {app} and pip-installs QuickCode into it
;   - Adds {app}\venv\Scripts to the user PATH, so `quickcode` and `qc` work
;   - Creates a Start Menu (and optional Desktop) shortcut launching the web
;     app through quickcode-app.exe - a GUI entry point, so no console window
;   - Optionally adds an "Open QuickCode here" Explorer folder context menu
;
; Build with the Inno Setup Compiler (ISCC.exe):
;   ISCC.exe packaging\quickcode.iss
;
; See packaging\README.md for full build instructions.
; ============================================================================

#define MyAppName "QuickCode"
; Override at compile time with /DMyAppVersion=<version> (scripts/release.py
; does this, reading pyproject.toml, so the two never drift apart). Falls
; back to this literal for a manual "Compile" from the Inno Setup IDE.
#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#define MyAppPublisher "Fichtel Systems"
#define MyAppURL "https://fichtelsystems.de"
#define MyAppContact "kontakt@fichtelsystems.de"

; RepoRoot is the QuickCode repository checkout this .iss lives in
; (packaging\quickcode.iss -> repo root is one level up).
#define RepoRoot ".."

; Everything the shortcuts, PATH and context menu point at lives in the venv
; that setup-quickcode.ps1 builds under {app}.
#define VenvScripts "{app}\venv\Scripts"
#define AppExeName "quickcode-app.exe"
#define CliExeName "quickcode.exe"

[Setup]
AppId={{6C8E6F2D-6E6A-4E6B-9C5A-2F5B1B8C6D3E}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
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
OutputDir=dist
OutputBaseFilename=QuickCode-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes

; Modern wizard chrome (Inno Setup 6.1+).
WizardStyle=modern

; The blue ghost: wizard icon, Add/Remove Programs entry, and shortcuts.
SetupIconFile=quickcode.ico
UninstallDisplayIcon={app}\quickcode.ico

; We modify PATH (a user environment variable), so Explorer and other
; processes need to be told the environment changed.
ChangesEnvironment=yes

; No architecture-specific binaries are shipped; QuickCode is pure Python.
ArchitecturesInstallIn64BitMode=x64compatible

VersionInfoVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} installer
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "addtopath"; Description: "Add QuickCode to my PATH (recommended)"; GroupDescription: "Additional tasks:"; Flags: checkedonce
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional tasks:"; Flags: unchecked
Name: "contextmenu"; Description: "Add ""Open QuickCode here"" to the folder right-click menu"; GroupDescription: "Explorer integration:"; Flags: unchecked

[Files]
; Bundle the full QuickCode source tree (everything pip needs to build/install
; the package) plus the packaging scripts, excluding dev/build cruft.
Source: "{#RepoRoot}\quickcode\*"; DestDir: "{app}\src\quickcode"; Excludes: "__pycache__,*.pyc"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#RepoRoot}\pyproject.toml"; DestDir: "{app}\src"; Flags: ignoreversion
Source: "{#RepoRoot}\README.md"; DestDir: "{app}\src"; Flags: ignoreversion
Source: "{#RepoRoot}\LICENSE"; DestDir: "{app}\src"; Flags: ignoreversion
Source: "{#RepoRoot}\scripts\bootstrap.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "{#RepoRoot}\scripts\install.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "setup-quickcode.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion
; The brand mark, kept at a stable path for shortcuts and the context menu.
Source: "quickcode.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; The web app, launched through the GUI entry point (pythonw): the browser
; opens on the user's home directory without a console window behind it.
Name: "{group}\{#MyAppName}"; Filename: "{#VenvScripts}\{#AppExeName}"; WorkingDir: "{%USERPROFILE}"; IconFilename: "{app}\quickcode.ico"; Comment: "Launch QuickCode"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{#VenvScripts}\{#AppExeName}"; WorkingDir: "{%USERPROFILE}"; IconFilename: "{app}\quickcode.ico"; Comment: "Launch QuickCode"; Tasks: desktopicon

[Registry]
; "Open QuickCode here" - right-click a folder. %V is the folder path, which
; the CLI accepts as its first positional argument (`quickcode "<dir>"`).
; HKCU only, and uninsdeletekey removes the whole key on uninstall.
Root: HKCU; Subkey: "Software\Classes\Directory\shell\QuickCode"; ValueType: string; ValueName: ""; ValueData: "Open QuickCode here"; Flags: uninsdeletekey; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\Directory\shell\QuickCode"; ValueType: string; ValueName: "Icon"; ValueData: "{app}\quickcode.ico"; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\Directory\shell\QuickCode\command"; ValueType: string; ValueName: ""; ValueData: """{#VenvScripts}\{#CliExeName}"" ""%V"""; Tasks: contextmenu
; And when right-clicking the empty background inside an open folder.
Root: HKCU; Subkey: "Software\Classes\Directory\Background\shell\QuickCode"; ValueType: string; ValueName: ""; ValueData: "Open QuickCode here"; Flags: uninsdeletekey; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\Directory\Background\shell\QuickCode"; ValueType: string; ValueName: "Icon"; ValueData: "{app}\quickcode.ico"; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\Directory\Background\shell\QuickCode\command"; ValueType: string; ValueName: ""; ValueData: """{#VenvScripts}\{#CliExeName}"" ""%V"""; Tasks: contextmenu

[Run]
; Post-install: ensure Git/Python, build the venv and pip-install QuickCode.
; Runs visibly (not hidden) so the user can see progress and any prompts
; (e.g. UAC for winget/installers).
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; \
    Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\scripts\setup-quickcode.ps1"" -SourceDir ""{app}\src"" -VenvDir ""{app}\venv"""; \
    WorkingDir: "{app}"; \
    StatusMsg: "Setting up Git, Python and QuickCode (this can take a few minutes)..."; \
    Flags: runascurrentuser waituntilterminated

; Offer to start the app straight from the last wizard page.
Filename: "{#VenvScripts}\{#AppExeName}"; \
    Description: "Launch {#MyAppName}"; \
    WorkingDir: "{%USERPROFILE}"; \
    Flags: nowait postinstall skipifsilent skipifdoesntexist

[UninstallDelete]
; pip and venv create files Inno Setup never logged, so remove the trees it
; would otherwise leave behind.
Type: filesandordirs; Name: "{app}\venv"
Type: filesandordirs; Name: "{app}\src"

[Code]
{ ---------------------------------------------------------------------------
  PATH handling: add the venv's Scripts directory, which is where pip puts
  quickcode.exe, qc.exe and quickcode-app.exe. Unlike the old `pip install
  --user` layout this is a single, exactly-known path, so there is nothing to
  guess and nothing left over for another Python install to inherit.
  --------------------------------------------------------------------------- }

const
  EnvironmentKey = 'Environment';

function VenvScriptsDir(): String;
begin
  Result := ExpandConstant('{app}\venv\Scripts');
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
      EnvAddPath(VenvScriptsDir());
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    EnvRemovePath(VenvScriptsDir());
end;
