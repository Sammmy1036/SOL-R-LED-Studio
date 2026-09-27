; SOL-R LED Studio installer (Inno Setup 6 - https://jrsoftware.org/isinfo.php)
;
; 1. Build the exe:   python -m PyInstaller --onefile --noconsole --name SolR-LED --icon SolR-LED.ico --version-file version_info.txt --collect-all customtkinter --collect-all libusb_package solr_led_studio.py
; 2. Put this file next to the "dist" folder, SolR-LED.ico and LICENSE.txt, and compile it in Inno Setup (or: ISCC.exe SolR-LED-Setup.iss)
; 3. Output: Output\SolR-LED-Setup.exe

#define AppName "SOL-R LED Studio"
#define AppVersion "1.0.0.0"
#define AppExe "SolR-LED.exe"

[Setup]
AppId={{6C1B6F4E-5B8A-4B7C-9E3D-2F1A0C9D7E55}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Sammmy1036
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputBaseFilename=SolR-LED-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\SolR-LED.ico
UninstallDisplayName={#AppName}
SetupIconFile=SolR-LED.ico
LicenseFile=LICENSE.txt
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}
VersionInfoDescription={#AppName} Setup
VersionInfoCopyright=
CloseApplications=force

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "SolR-LED.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "LICENSE.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; IconFilename: "{app}\SolR-LED.ico"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; IconFilename: "{app}\SolR-LED.ico"; Tasks: desktopicon

[Run]
; One-time LED driver setup for any sticks plugged in right now.
; Sticks plugged in later get a "Set up LED driver" button inside the app.
Filename: "{app}\{#AppExe}"; Parameters: "--install-driver"; StatusMsg: "Setting up the SOL-R LED driver..."; Flags: runhidden waituntilterminated
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/IM {#AppExe} /F"; Flags: runhidden; RunOnceId: "StopApp"
Filename: "{app}\{#AppExe}"; Parameters: "--uninstall-driver"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveDriver"

[UninstallDelete]
; Startup entry the app creates when "Start with Windows" is turned on in the app
Type: files; Name: "{userappdata}\Microsoft\Windows\Start Menu\Programs\Startup\SolR-LED.cmd"
