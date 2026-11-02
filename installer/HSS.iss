; Inno Setup script - per-user install, no admin rights. UNSIGNED (Windows SmartScreen will warn).
; Build:  ISCC.exe /DAppVersion=0.4.0 installer\HSS.iss        (input: dist\HSS from PyInstaller)
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "Hybrid Secure Storage"

[Setup]
; Fixed AppId => installing a newer build upgrades in place.
AppId={{6E1B6B0E-5F0B-4D55-9A52-7C1B0E5A3D21}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=HSS Project
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
OutputDir=..\release
OutputBaseFilename=HSS-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\HSS.exe
CloseApplications=yes
RestartApplications=no
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a &Desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startmenuicon"; Description: "Create a &Start Menu shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\HSS\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Dirs]
; Per-user data lives here and is NEVER removed by the uninstaller or touched by upgrades.
Name: "{localappdata}\HSS"; Flags: uninsneveruninstall

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\HSS.exe"; Tasks: startmenuicon
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\HSS.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\HSS.exe"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

; No [UninstallDelete] entry for {localappdata}\HSS on purpose (data preserved on uninstall).
