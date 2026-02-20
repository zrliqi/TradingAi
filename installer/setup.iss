[Setup]
AppName=TradingAi
AppVersion=0.1.8
DefaultDirName={pf}\TradingAi
DefaultGroupName=TradingAi
OutputDir=.
OutputBaseFilename=TradingAi_Setup
Compression=lzma
SolidCompression=yes
DisableProgramGroupPage=yes
SetupIconFile=..\logo.ico
UninstallDisplayIcon={app}\logo.ico

[Files]
Source: "..\dist\TradingAi.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\logo.ico"; DestDir: "{app}"; Flags: ignoreversion


[Icons]
Name: "{group}\TradingAi"; Filename: "{app}\TradingAi.exe"; IconFilename: "{app}\logo.ico"
Name: "{commondesktop}\TradingAi"; Filename: "{app}\TradingAi.exe"; Tasks: desktopicon; IconFilename: "{app}\logo.ico"

[Tasks]
Name: "desktopicon"; Description: "Create a Desktop shortcut"; GroupDescription: "Additional icons:"

[Run]
Filename: "{app}\TradingAi.exe"; Description: "Launch TradingAi"; Flags: nowait postinstall skipifsilent