#ifndef AppVersion
  #error Supply the release version using /DAppVersion
#endif
#define AppName "VK3FS Beacon Strength Plotter"
#define AppExe "Icom S-meter Plotter.exe"

[Setup]
AppId=VK3FS.BeaconStrengthPlotter
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=VK3FS
DefaultDirName={localappdata}\Programs\VK3FS Beacon Strength Plotter
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
DisableStartupPrompt=yes
DisableProgramGroupPage=yes
CloseApplications=yes
RestartApplications=no
SetupIconFile=..\radio_tower.ico
UninstallDisplayIcon={app}\{#AppExe}
OutputDir=..\installer-output
OutputBaseFilename=VK3FS-Beacon-Strength-Plotter-Setup
WizardStyle=modern
SolidCompression=yes

[Files]
Source: "..\dist\{#AppExe}"; DestDir: "{app}"
Source: "..\docs\Beacon-Plotter-Quick-Start.pdf"; DestDir: "{app}\docs"
Source: "..\docs\Beacon-Plotter-Operating-Guide.pdf"; DestDir: "{app}\docs"
Source: "..\OpenSans-LICENSE.txt"; DestDir: "{app}"

[Icons]
Name: "{userprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{userprograms}\VK3FS Beacon Strength Plotter Quick Start"; Filename: "{app}\docs\Beacon-Plotter-Quick-Start.pdf"

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
