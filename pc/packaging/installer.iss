#define AppVersion "0.10.0"
[Setup]
AppId={{98D80667-39A4-498E-8BCC-754A6285566A}
AppName=Kkullim PC
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\Kkullim
DefaultGroupName=Kkullim PC
PrivilegesRequired=lowest
OutputDir=..\release
OutputBaseFilename=Kkullim-PC-{#AppVersion}-Setup-x64
SetupIconFile=..\app_icon.ico
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
UninstallDisplayIcon={app}\Kkullim.exe
CloseApplications=yes
[Files]
Source: "..\dist\Kkullim\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{group}\Kkullim PC"; Filename: "{app}\Kkullim.exe"
Name: "{autodesktop}\Kkullim PC"; Filename: "{app}\Kkullim.exe"
[Run]
Filename: "{app}\Kkullim.exe"; Description: "Launch Kkullim PC"; Flags: nowait postinstall skipifsilent
