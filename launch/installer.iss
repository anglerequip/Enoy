#ifndef AppVersion
  #define AppVersion "0.5.0"
#endif

[Setup]
AppId={{3C9836CE-5276-4271-AF30-718BFAD2A58E}
AppName=Enoy
AppVersion={#AppVersion}
AppPublisher=anglerequip
AppPublisherURL=https://github.com/anglerequip/Enoy
AppUpdatesURL=https://github.com/anglerequip/Enoy/releases
DefaultDirName={localappdata}\Programs\Enoy
DefaultGroupName=Enoy
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir=..\dist
OutputBaseFilename=Enoy-Setup-x64
SetupIconFile=enoy.ico
UninstallDisplayIcon={app}\Enoy.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
CloseApplicationsFilter=Enoy.exe,Enoy-tools.exe
RestartApplications=no
DisableProgramGroupPage=yes
UsePreviousTasks=yes
Uninstallable=not IsSmokeTest

[Tasks]
Name: desktopicon; Description: "Create a desktop shortcut"; Flags: checkedonce
Name: providers; Description: "Install missing Codex, Claude Code and WebView2 (internet required)"; Flags: checkedonce; Check: not IsUpdate

[Files]
Source: "..\dist\Enoy\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Enoy"; Filename: "{app}\Enoy.exe"; WorkingDir: "{app}"; AppUserModelID: "Enoy.Desktop"; Check: not IsSmokeTest
Name: "{autodesktop}\Enoy"; Filename: "{app}\Enoy.exe"; WorkingDir: "{app}"; AppUserModelID: "Enoy.Desktop"; Tasks: desktopicon; Check: not IsSmokeTest

[Run]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoLogo -NoProfile -ExecutionPolicy Bypass -File ""{app}\_internal\launch\install-providers.ps1"""; Description: "Set up missing tools"; StatusMsg: "Installing missing tools from their official publishers..."; Flags: runasoriginaluser; Tasks: providers; Check: not IsUpdate
Filename: "{app}\Enoy.exe"; Description: "Open Enoy and sign into your accounts"; Flags: nowait postinstall skipifsilent runasoriginaluser
Filename: "{app}\Enoy.exe"; Flags: nowait runasoriginaluser; Check: IsUpdate

[Code]
function IsUpdate: Boolean;
begin
  Result := ExpandConstant('{param:ENOYUPDATE|0}') <> '0';
end;

function IsSmokeTest: Boolean;
begin
  Result := ExpandConstant('{param:ENOYTEST|0}') <> '0';
end;
