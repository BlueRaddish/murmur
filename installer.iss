; Inno Setup script. Per-user install, no admin. Adds a Start Menu entry and a
; run-at-login task, and offers to launch it.
#define AppVersion "0.8.0"
[Setup]
AppId={{7C1F0D8A-6C2B-4E1E-9B57-MURMUR000001}
AppName=murmur
AppVersion={#AppVersion}
AppPublisher=BlueRaddish
AppPublisherURL=https://github.com/BlueRaddish/murmur
DefaultDirName={localappdata}\Programs\murmur
DefaultGroupName=murmur
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=murmur-setup
SetupIconFile=murmur.ico
UninstallDisplayIcon={app}\murmur.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
CloseApplications=yes

[Tasks]
Name: "startup"; Description: "Start murmur when I sign in"; GroupDescription: "Startup:"

[Files]
Source: "dist\murmur\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs

[Icons]
Name: "{group}\murmur"; Filename: "{app}\murmur.exe"
Name: "{group}\Uninstall murmur"; Filename: "{uninstallexe}"
Name: "{userstartup}\murmur"; Filename: "{app}\murmur.exe"; Tasks: startup

[Run]
Filename: "{app}\murmur.exe"; Description: "Start murmur now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "taskkill"; Parameters: "/f /im murmur.exe"; Flags: runhidden; RunOnceId: "killmurmur"
