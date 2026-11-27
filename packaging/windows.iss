; Inno Setup 6 离线安装器脚本（无需网络、无在线组件下载）
; 升级/卸载/图标刷新全部由安装程序自己完成，用户只需双击安装包。
#define MyAppName "轻述 sayo"
; 版本号唯一来源是 app/config.py 的 APP_VERSION，由 scripts/build_windows.ps1 通过
; ISCC /DMyAppVersion=... 注入；直接手动编译 ISCC 时才用下面的兜底值。
#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#define MyAppExeName "sayo.exe"

[Setup]
AppId={{6E2D9E8A-3A31-4E5B-9C6F-1D9A7B0C4E21}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={autopf}\sayo
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=sayo-{#MyAppVersion}-windows-setup
Compression=lzma2/max
SolidCompression=yes
; 安装包本身与卸载程序都用品牌图标（深色圆角 + 蓝紫渐变 S），一眼可辨
SetupIconFile=..\assets\icon-windows.ico
; 控制面板/设置→应用 里只显示“轻述 sayo”，不带“版本 1.0.0”这种冗余后缀
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableWelcomePage=no
PrivilegesRequired=lowest
; 覆盖安装时自动处理正在运行的旧实例，用户不需要先手动退出
CloseApplications=yes
CloseApplicationsFilter={#MyAppExeName}
RestartApplications=no
; 记住上次安装目录，升级时原地覆盖，不产生第二份程序
UsePreviousAppDir=yes
SetupLogging=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[InstallDelete]
; 清掉旧版本遗留的额外文件（例如历史上的模型安装脚本目录），保证安装目录干净
Type: filesandordirs; Name: "{app}\tools"
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\install_models.bat"
Type: files; Name: "{app}\install_models.ps1"
; 上一次安装把卸载程序改名成了“卸载sayo.*”，这里先清掉，避免升级后残留两份卸载程序
Type: files; Name: "{app}\卸载sayo.exe"
Type: files; Name: "{app}\卸载sayo.dat"
Type: files; Name: "{app}\卸载sayo.msg"

[Files]
Source: "..\dist\sayo\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[UninstallDelete]
; 卸载后不留空目录：只有目录已经空了才会被删掉，用户数据（模型、配置）不受影响
Type: dirifempty; Name: "{app}"
Type: dirifempty; Name: "{app}\_internal"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动"; Flags: nowait postinstall skipifsilent

[Code]
const
  SHCNE_ASSOCCHANGED = $08000000;
  SHCNF_IDLIST = $0000;
  { 卸载程序改名后的基名：安装目录里不再出现 unins000.exe / unins000.dat 这种原始名字 }
  UninstallerBaseName = '卸载sayo';
  UninstallRegKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{6E2D9E8A-3A31-4E5B-9C6F-1D9A7B0C4E21}_is1';

procedure SHChangeNotify(wEventId: LongInt; uFlags: Cardinal;
  dwItem1, dwItem2: LongInt); external 'SHChangeNotify@shell32.dll stdcall';

{ 改名单个文件；源文件不存在视为成功（.msg 这类文件不是每次都有） }
function MoveIfExists(const OldFile, NewFile: String): Boolean;
begin
  Result := True;
  if not FileExists(OldFile) then
    Exit;
  if FileExists(NewFile) then
    DeleteFile(NewFile);
  Result := RenameFile(OldFile, NewFile);
end;

{ 把 unins000.exe / unins000.dat 改名成“卸载sayo.exe / 卸载sayo.dat”，
  并同步改写注册表里的卸载入口，保证「设置 → 应用 → 卸载」依然可用 }
procedure RenameUninstaller;
var
  OldExe, OldDat, OldMsg, NewExe, Cmd: String;
  Root: Integer;
begin
  OldExe := ExpandConstant('{uninstallexe}');
  NewExe := ExpandConstant('{app}\') + UninstallerBaseName + '.exe';
  if CompareText(OldExe, NewExe) = 0 then
    Exit;

  OldDat := ChangeFileExt(OldExe, '.dat');
  OldMsg := ChangeFileExt(OldExe, '.msg');

  { .dat 是卸载数据文件，必须和 .exe 同名同步改名；先搬数据，最后搬 exe }
  if not MoveIfExists(OldDat, ChangeFileExt(NewExe, '.dat')) then
    Exit;
  MoveIfExists(OldMsg, ChangeFileExt(NewExe, '.msg'));

  if not MoveIfExists(OldExe, NewExe) then
  begin
    { exe 改名失败就把 .dat 还原，避免卸载程序找不到自己的数据文件 }
    MoveIfExists(ChangeFileExt(NewExe, '.dat'), OldDat);
    Exit;
  end;

  if RegQueryStringValue(HKCU, UninstallRegKey, 'UninstallString', Cmd) then
    Root := HKCU
  else if RegQueryStringValue(HKLM, UninstallRegKey, 'UninstallString', Cmd) then
    Root := HKLM
  else
    Exit;

  Cmd := '"' + NewExe + '"';
  RegWriteStringValue(Root, UninstallRegKey, 'UninstallString', Cmd);
  RegWriteStringValue(Root, UninstallRegKey, 'QuietUninstallString', Cmd + ' /SILENT');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  // 静默关掉正在运行的旧实例，避免「文件被占用，请重启」这种需要用户操作的提示
  Exec('taskkill.exe', '/F /IM {#MyAppExeName}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    // 安装完立即刷新资源管理器图标缓存：桌面图标直接就是新的圆角品牌图标，
    // 用户不需要重启资源管理器，更不需要注销。
    Exec('ie4uinit.exe', '-ClearIconCache', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, 0, 0);
  end
  else if CurStep = ssDone then
  begin
    // 此时卸载程序已经生成，改成中文名字（用户看到的是“卸载sayo”）
    RenameUninstaller;
  end;
end;
