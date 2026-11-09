#define AppVersion "0.2.0"
[Setup]
AppId={{30FAE268-51BD-4C34-9377-B47091018D93}
AppName=Coord
AppVersion={#AppVersion}
AppPublisher=APG
DefaultDirName={localappdata}\Programs\APG\Coord
DefaultGroupName=APG
PrivilegesRequired=lowest
OutputDir=..\..\dist\installer
OutputBaseFilename=Coord-Setup-{#AppVersion}-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\Coord.exe
[Files]
Source: "..\..\dist\Coord\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{group}\Coord"; Filename: "{app}\Coord.exe"
Name: "{autodesktop}\Coord"; Filename: "{app}\Coord.exe"; Tasks: desktopicon
[Tasks]
Name: desktopicon; Description: "Create a desktop shortcut"; Flags: unchecked
[Run]
Filename: "{app}\Coord.exe"; Description: "Open Coord"; Flags: nowait postinstall skipifsilent
