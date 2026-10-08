; Inno Setup script for Busyist. Built by build.ps1:
;   ISCC.exe /DAppVersion=1.0.0 packaging\busyist.iss
; Installs for the current user only (no admin prompt) into
; %LOCALAPPDATA%\Programs\Busyist.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6E0F3C1B-8E2A-4C1D-9B57-3D4F5A6B7C81}
AppName=Busyist
AppVersion={#AppVersion}
AppVerName=Busyist {#AppVersion}
AppPublisher=Busyist
AppPublisherURL=https://github.com/josedaidone/busyist
AppSupportURL=https://github.com/josedaidone/busyist/issues
AppUpdatesURL=https://github.com/josedaidone/busyist/releases
DefaultDirName={autopf}\Busyist
DefaultGroupName=Busyist
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=Busyist-Setup-{#AppVersion}
SetupIconFile=..\busyist.ico
UninstallDisplayIcon={app}\Busyist.exe
UninstallDisplayName=Busyist
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
CloseApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "..\dist\Busyist\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Microsoft's WebView2 bootstrapper (~2 MB), only run when the runtime is missing.
Source: "..\build\MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: NeedsWebView2

[Icons]
Name: "{autoprograms}\Busyist"; Filename: "{app}\Busyist.exe"
Name: "{autodesktop}\Busyist"; Filename: "{app}\Busyist.exe"; Tasks: desktopicon

[Run]
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "Installing the Microsoft Edge WebView2 Runtime..."; Check: NeedsWebView2; Flags: waituntilterminated
Filename: "{app}\Busyist.exe"; Parameters: "--show"; Description: "Start Busyist now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM Busyist.exe"; Flags: runhidden; RunOnceId: "StopBusyist"

[Registry]
; The app adds itself to sign-in (a setting in the app); remove that on uninstall.
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
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM Busyist.exe', '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopRunningApp;  { an update replaces files the running copy has open }
  Result := '';
end;

{ The app's own updater runs Setup with /VERYSILENT /RELAUNCH=1 after quitting.
  Start the app again at the end, whether or not the update went through, so
  a failed update doesn't leave Busyist closed. }
procedure DeinitializeSetup;
var
  AppExe: String;
  Code: Integer;
begin
  if ExpandConstant('{param:RELAUNCH|0}') <> '1' then
    Exit;
  try
    AppExe := ExpandConstant('{app}\Busyist.exe');
  except
    Exit;  { Setup stopped before it knew where the app is }
  end;
  if FileExists(AppExe) then
    ExecAsOriginalUser(AppExe, '', '', SW_SHOWNORMAL, ewNoWait, Code);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{userappdata}\Busyist');
    if DirExists(DataDir) and not UninstallSilent and
       (MsgBox('Also delete your Busyist settings (Todoist token, bar address, saved filters) and history?' + #13#10 + #13#10 + DataDir,
               mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES) then
      DelTree(DataDir, True, True, True);
  end;
end;
