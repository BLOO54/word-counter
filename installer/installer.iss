; Inno Setup script -> WordCounter-Setup.exe
; Installs for the current user only, so it NEVER needs administrator rights.

#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#define MyAppName "Word Counter"
#define MyAppExe "WordCounter.exe"

[Setup]
AppId={{B6E2A9D4-3F71-4C58-9E0A-7D41C2F8A563}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Word Counter
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\Output
OutputBaseFilename=WordCounter-Setup-{#MyAppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExe}
; fast compression: builds in ~25 s instead of ~80 s (installer is a bit bigger)
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\WordCounter\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{autoprograms}\{#MyAppName} (Self test)"; Filename: "{app}\{#MyAppExe}"; Parameters: "--self-test"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "Start {#MyAppName} now"; Flags: nowait postinstall skipifsilent
; after an in-app update (silent install started with /RESTARTAPP=1) start the new version again
Filename: "{app}\{#MyAppExe}"; Flags: nowait; Check: ShouldRelaunch

[Code]
function ShouldRelaunch: Boolean;
begin
  Result := ExpandConstant('{param:RESTARTAPP|0}') = '1';
end;

[UninstallRun]
Filename: "{cmd}"; Parameters: "/C taskkill /IM {#MyAppExe} /F"; Flags: runhidden; RunOnceId: "KillApp"
