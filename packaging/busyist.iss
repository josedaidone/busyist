; Inno Setup script for buZyist. Built by build.ps1:
;   ISCC.exe /DAppVersion=1.0.0 packaging\busyist.iss
; Installs for the current user only (no admin prompt) into
; %LOCALAPPDATA%\Programs\Buzyist.
;
; The app was called Busyist before 1.5.0. The AppId stays the same so this
; installs over an old copy (in its old folder, Inno reuses the previous
; directory), the data folder stays %APPDATA%\Busyist (Chrome loads the
; extension from there) and so does the "Busyist" sign-in Run value. The old
; Busyist.exe and its shortcuts are removed.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6E0F3C1B-8E2A-4C1D-9B57-3D4F5A6B7C81}
AppName=buZyist
AppVersion={#AppVersion}
AppVerName=buZyist {#AppVersion}
AppPublisher=buZyist
AppPublisherURL=https://github.com/josedaidone/busyist
AppSupportURL=https://github.com/josedaidone/busyist/issues
AppUpdatesURL=https://github.com/josedaidone/busyist/releases
DefaultDirName={autopf}\Buzyist
DefaultGroupName=buZyist
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=Buzyist-Setup-{#AppVersion}
SetupIconFile=..\busyist.ico
UninstallDisplayIcon={app}\Buzyist.exe
UninstallDisplayName=buZyist
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
CloseApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "..\dist\Buzyist\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Microsoft's WebView2 bootstrapper (~2 MB), only run when the runtime is missing.
Source: "..\build\MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: NeedsWebView2

[InstallDelete]
; Left over from before the rename.
Type: files; Name: "{app}\Busyist.exe"
Type: files; Name: "{autoprograms}\Busyist.lnk"
Type: files; Name: "{autodesktop}\Busyist.lnk"

[Icons]
Name: "{autoprograms}\buZyist"; Filename: "{app}\Buzyist.exe"
Name: "{autodesktop}\buZyist"; Filename: "{app}\Buzyist.exe"; Tasks: desktopicon

[Run]
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "Installing the Microsoft Edge WebView2 Runtime..."; Check: NeedsWebView2; Flags: waituntilterminated
Filename: "{app}\Buzyist.exe"; Parameters: "--show"; Description: "Start buZyist now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM Buzyist.exe /IM Busyist.exe"; Flags: runhidden; RunOnceId: "StopBusyist"

[Registry]
; The app adds itself to sign-in (a setting in the app); remove that on uninstall.
; The value keeps its pre-rename name, "Busyist".
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "Busyist"; ValueType: none; Flags: uninsdeletevalue dontcreatekey

[Code]
const
  WebView2Client = '\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function HasRuntime(Root: Integer; Key: String): Boolean;
var
  Version: String;
begin
  Result := RegQueryStringValue(Root, Key, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0');
end;

function NeedsWebView2: Boolean;
begin
  Result := not (HasRuntime(HKLM, 'SOFTWARE\WOW6432Node' + WebView2Client) or
                 HasRuntime(HKLM, 'SOFTWARE' + WebView2Client) or
                 HasRuntime(HKCU, 'Software' + WebView2Client));
end;

procedure StopRunningApp;
var
  Code: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM Buzyist.exe /IM Busyist.exe', '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopRunningApp;  { an update replaces files the running copy has open }
  Result := '';
end;

{ The app's own updater runs Setup with /VERYSILENT /RELAUNCH=1 after quitting.
  Start the app again at the end, whether or not the update went through, so
  a failed update doesn't leave buZyist closed. }
procedure DeinitializeSetup;
var
  AppExe: String;
  Code: Integer;
begin
  if ExpandConstant('{param:RELAUNCH|0}') <> '1' then
    Exit;
  try
    AppExe := ExpandConstant('{app}\Buzyist.exe');
  except
    Exit;  { Setup stopped before it knew where the app is }
  end;
  if FileExists(AppExe) then
    ExecAsOriginalUser(AppExe, '', '', SW_SHOWNORMAL, ewNoWait, Code);
end;

{ A sign-in entry from before the rename points at Busyist.exe, which is gone
  now. Point it at Buzyist.exe (the app also does this when it starts). }
procedure CurStepChanged(CurStep: TSetupStep);
var
  Command: String;
begin
  if CurStep <> ssPostInstall then
    Exit;
  if RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'Busyist', Command) and
     (Pos('busyist.exe', Lowercase(Command)) > 0) then
    RegWriteStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'Busyist',
                        '"' + ExpandConstant('{app}\Buzyist.exe') + '"');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{userappdata}\Busyist');
    if DirExists(DataDir) and not UninstallSilent and
       (MsgBox('Also delete your buZyist settings (Todoist token, bar address, saved filters) and history?' + #13#10 + #13#10 + DataDir,
               mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES) then
      DelTree(DataDir, True, True, True);
  end;
end;
