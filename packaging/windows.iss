; 可选：Inno Setup 6 离线安装器脚本（无需网络、无在线组件下载）
#define MyAppName "轻述 sayo"
#define MyAppVersion "1.0.0"
#define MyAppExeName "sayo.exe"

[Setup]
AppId={{6E2D9E8A-3A31-4E5B-9C6F-1D9A7B0C4E21}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={autopf}\sayo
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=sayo-1.0.0-windows-setup
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 明确关闭一切联网行为
DisableWelcomePage=no
AllowNetworkDownloads=no
PrivilegesRequired=lowest

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "..\dist\sayo\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动（完全离线）"; Flags: nowait postinstall skipifsilent
