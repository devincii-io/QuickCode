; ============================================================================
; QuickCode - Inno Setup installer script
;
; Builds a per-user Windows installer that copies the frozen PyInstaller
; onedir build (dist\QuickCode, see quickcode.spec at the repo root) into
; %LOCALAPPDATA%\Programs\QuickCode. Nothing is compiled, downloaded or
; pip-installed on the user's machine: no Git, no Python, no network, no venv.
;
; What ships in that folder:
;   QuickCodeApp.exe  the windowed app (Start Menu, desktop, context menu)
;   quickcode.exe     the console CLI (`quickcode -p`, `quickcode doctor`)
;   qc.cmd            the short alias, forwarding to quickcode.exe
;   _internal\        the Python runtime, dependencies and the frontend
;
; Build with the Inno Setup Compiler (ISCC.exe), after PyInstaller has run:
;   pyinstaller --noconfirm --clean quickcode.spec
;   ISCC.exe /DMyAppVersion=<version> packaging\quickcode.iss
;
; scripts\release.py --build does both in order. See packaging\README.md.
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
; The PyInstaller COLLECT output. Its name comes from quickcode.spec.
#define FrozenDir RepoRoot + "\dist\QuickCode"

; The windowed entry point cannot be called QuickCode.exe: Windows file names
; are case-insensitive, so it would be the same file as quickcode.exe. The
; console name is the one that had to survive, because it is what a user types.
#define AppExeName "QuickCodeApp.exe"
#define CliExeName "quickcode.exe"

[Setup]
; Never change this GUID: it is what Windows uses to recognise an existing
; QuickCode install. A new one orphans every copy already out there.
AppId={{6C8E6F2D-6E6A-4E6B-9C5A-2F5B1B8C6D3E}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL=mailto:{#MyAppContact}
AppUpdatesURL={#MyAppURL}
AppContact={#MyAppContact}

; Per-user install: no elevation, and the same directory {autopf} already
; resolved to under PrivilegesRequired=lowest, so an upgrade lands in place.
DefaultDirName={localappdata}\Programs\{#MyAppName}
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
UninstallDisplayIcon={app}\{#AppExeName}

; We modify PATH (a user environment variable), so Explorer and other
; processes need to be told the environment changed.
ChangesEnvironment=yes

; The frozen build embeds a 64-bit Python runtime and CPython extension
; modules, so unlike the old source-copying installer this is not portable.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; Shut a running copy down before overwriting its own executables.
CloseApplications=yes
RestartApplications=no

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

[InstallDelete]
; Upgrading over an older install, which put a private virtual environment and
; a copy of the source tree here. Nothing in the frozen build reads either, and
; leaving them behind would strand ~150 MB the uninstaller no longer knows about.
Type: filesandordirs; Name: "{app}\venv"
Type: filesandordirs; Name: "{app}\src"
Type: filesandordirs; Name: "{app}\scripts"
; The previous version's distribution metadata. `importlib.metadata` answers
; from whatever `*.dist-info` it finds under `_internal`, and the new one is
; called something else (quickcode-2.4.0.dist-info, not 2.3.0), so nothing
; overwrites it -- leaving the upgraded app to report the version it replaced
; to `--version`, `/api/health` and the update check, which then offers the
; same update again for ever.
Type: filesandordirs; Name: "{app}\_internal\quickcode-*.dist-info"

[Files]
; The whole PyInstaller onedir tree: both executables plus _internal\.
Source: "{#FrozenDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; `qc`, the short alias. A .cmd rather than a third 10 MB executable.
Source: "qc.cmd"; DestDir: "{app}"; Flags: ignoreversion
; The brand mark, kept at a stable path for the context menu icon.
Source: "quickcode.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#RepoRoot}\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#RepoRoot}\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#RepoRoot}\THIRD-PARTY-NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; The windowed entry point: the app window opens on the user's home directory
; with no console behind it.
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{%USERPROFILE}"; Comment: "Launch QuickCode"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{%USERPROFILE}"; Comment: "Launch QuickCode"; Tasks: desktopicon

[Registry]
; "Open QuickCode here" - right-click a folder. %V is the folder path, which
; the windowed entry point takes as its project directory (see cli.main_app:
; a folder handed in this way wins over the home-directory default).
; HKCU only, and uninsdeletekey removes the whole key on uninstall.
Root: HKCU; Subkey: "Software\Classes\Directory\shell\QuickCode"; ValueType: string; ValueName: ""; ValueData: "Open QuickCode here"; Flags: uninsdeletekey; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\Directory\shell\QuickCode"; ValueType: string; ValueName: "Icon"; ValueData: "{app}\quickcode.ico"; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\Directory\shell\QuickCode\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" ""%V"""; Tasks: contextmenu
; And when right-clicking the empty background inside an open folder.
Root: HKCU; Subkey: "Software\Classes\Directory\Background\shell\QuickCode"; ValueType: string; ValueName: ""; ValueData: "Open QuickCode here"; Flags: uninsdeletekey; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\Directory\Background\shell\QuickCode"; ValueType: string; ValueName: "Icon"; ValueData: "{app}\quickcode.ico"; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\Directory\Background\shell\QuickCode\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" ""%V"""; Tasks: contextmenu

[Run]
; Offer to start the app straight from the last wizard page. No post-install
; setup step exists any more -- the files being copied *is* the install.
Filename: "{app}\{#AppExeName}"; \
    Description: "Launch {#MyAppName}"; \
    WorkingDir: "{%USERPROFILE}"; \
    Flags: nowait postinstall skipifsilent skipifdoesntexist

[UninstallDelete]
; PyInstaller's onedir tree is fully logged by Inno Setup, but the app writes
; a WebView2 profile and session state under %USERPROFILE%\.quickcode, which is
; the user's data and is deliberately left alone. Only the install dir goes.
Type: dirifempty; Name: "{app}"

[Code]
{ ---------------------------------------------------------------------------
  PATH handling: add the install directory itself, which is where the frozen
  quickcode.exe and qc.cmd live. Older installs put the venv Scripts directory
  under it on PATH instead; that directory is deleted by InstallDelete above,
  so the stale entry is removed here rather than left pointing at nothing.
  --------------------------------------------------------------------------- }

const
  EnvironmentKey = 'Environment';

function LegacyVenvScriptsDir(): String;
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
    { Unconditional: the directory it names no longer exists after this run. }
    EnvRemovePath(LegacyVenvScriptsDir());
    if IsTaskSelected('addtopath') then
      EnvAddPath(ExpandConstant('{app}'));
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    EnvRemovePath(ExpandConstant('{app}'));
    EnvRemovePath(LegacyVenvScriptsDir());
  end;
end;

{ ------------------------------------------------------------------------
  Closing a running QuickCode before overwriting it.

  `CloseApplications=yes` asks Windows' Restart Manager to do this, and for a
  WebView2 window it is not enough: RM asks politely, the window does not
  answer in time, and Setup falls through to "DeleteFile failed; code 5 --
  access denied" on QuickCodeApp.exe, mid-install. The update path makes that
  the *normal* case rather than an edge one, because the app downloads the
  installer and launches it as a detached child while continuing to run — so
  every in-app update hit this.

  Asked, not assumed: closing someone's editor without warning is worse than
  a failed install. Declining is a clean abort, not a half-written directory.

  NO /T. That flag kills the target's whole process tree, and when the update
  is started from inside QuickCode the installer is a *child of the very
  process being killed* -- so Setup handed taskkill a tree it was standing in
  and was terminated by its own cleanup. Killing by image name reaches every
  copy of the app, which is all that is needed: what holds the lock on
  QuickCodeApp.exe is QuickCodeApp.exe. (`update.py` now also starts the
  installer detached, so it is not in that tree either. Both, because either
  one alone leaves this working by accident.)
  ------------------------------------------------------------------------ }
function TerminateExe(const ExeName: String): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM ' + ExeName,
                 '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  { 128 = "no such process", which is the outcome we want anyway. }
  Result := Result and ((ResultCode = 0) or (ResultCode = 128));
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if WizardSilent() then
  begin
    { An unattended run has nobody to ask, and a silent install that fails
      halfway is worse than one that closes the app it is replacing. }
    TerminateExe('QuickCodeApp.exe');
    TerminateExe('quickcode.exe');
    Sleep(700);
    Exit;
  end;
  if MsgBox('QuickCode is being replaced, so any running copy has to close first.'
            + #13#10#13#10 + 'Close QuickCode now and continue?',
            mbConfirmation, MB_YESNO) = IDNO then
  begin
    Result := 'Setup was cancelled: QuickCode has to be closed before it can be updated.';
    Exit;
  end;
  if not TerminateExe('QuickCodeApp.exe') then
    Result := 'QuickCode could not be closed automatically. Close it and run this installer again.';
  if Result = '' then
    if not TerminateExe('quickcode.exe') then
      Result := 'A QuickCode command line could not be closed. Close it and run this installer again.';
  { Give Windows a moment to release the image locks before the copy starts. }
  if Result = '' then
    Sleep(700);
end;
