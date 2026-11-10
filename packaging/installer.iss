; 本地多维表格管理器 Inno Setup 6 安装脚本。
; 通常由 packaging\build.ps1 调用，并传入 /DDistDir、/DOutputDir、/DAppVersion：
;   ISCC.exe /DDistDir=..\build\local_sql_manager.dist packaging\installer.iss
; 也可直接用 Inno Setup 打开本文件手工编译。

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

#ifndef DistDir
  #define DistDir "..\build\local_sql_manager.dist"
#endif

#ifndef OutputDir
  #define OutputDir "..\build\installer"
#endif

#define AppName "本地多维表格管理器"
#define AppExeName "local-sql-manager.exe"

[Setup]
AppId={{A6A90388-112C-45B4-B24D-A53B981694AA}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=LocalSQLManager
DefaultDirName={autopf}\LocalSQLManager
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename=local-sql-manager-setup
SetupIconFile=app.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=admin
; 与应用内的单实例互斥体同名，运行中安装会提示先退出
AppMutex=LocalSQLManager.SingleInstance

[Languages]
; 官方 Inno Setup 6 不含简体中文语言文件：存在时由 build.ps1 传入 /DChineseLang。
#ifdef ChineseLang
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
#else
Name: "english"; MessagesFile: "compiler:Default.isl"
#endif

[Files]
; 整个 Nuitka 目录版原样安装，包含 local_sql_manager\templates 与 static。
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "启动{#AppName}"; Flags: nowait postinstall skipifsilent

; 卸载说明：Inno Setup 只删除自己安装进 {app} 的文件。
; 用户数据不在安装目录内，卸载后保留：
;   %LOCALAPPDATA%\LocalSQLManager   配置与日志
;   文档\LocalSQLManager             用户数据库
; 因此这里刻意不配置 [UninstallDelete] 指向上述目录。
