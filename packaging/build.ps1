<#
.SYNOPSIS
  构建「本地多维表格」Windows 目录版（Nuitka）与安装包（Inno Setup 6）。

.DESCRIPTION
  在仓库根目录执行：
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1
  产出：
    build\local_sql_manager.dist\local-sql-manager.exe   目录版
    build\installer\local-sql-manager-setup.exe            安装包（未加 -SkipInstaller 时）

.PARAMETER SkipInstaller
  只运行 Nuitka，跳过 Inno Setup 编译。

.PARAMETER Clean
  构建前删除整个 build\ 目录（含旧产物与 Nuitka 缓存）。

.PARAMETER IsccPath
  指定 ISCC.exe 路径；默认从 PATH 与常见安装位置探测。

.PARAMETER Python
  指定解释器；默认使用仓库内 .venv\Scripts\python.exe。

.PARAMETER Version
  exe 文件版本 / 产品版本 / 安装包版本，默认 1.0.0。
#>
[CmdletBinding()]
param(
    [switch]$SkipInstaller,
    [switch]$Clean,
    [string]$IsccPath = "",
    [string]$Python = "",
    [string]$Version = "1.0.0"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Fail([string]$Message) {
    Write-Host ""
    Write-Host "构建失败：$Message" -ForegroundColor Red
    exit 1
}

if (-not $Python) {
    $Python = Join-Path $Root ".venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $Python)) {
    Fail "找不到 Python 解释器：$Python。请先在仓库根目录执行 uv venv 与 uv pip install -e .，或用 -Python 指定解释器。"
}

$BuildDir = Join-Path $Root "build"
$DistDir = Join-Path $BuildDir "local_sql_manager.dist"
$InstallerDir = Join-Path $BuildDir "installer"

if ($Clean -and (Test-Path -LiteralPath $BuildDir)) {
    Write-Step "清理 build\"
    Remove-Item -LiteralPath $BuildDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null

foreach ($relative in @("local_sql_manager\templates", "local_sql_manager\static")) {
    $full = Join-Path $Root $relative
    if (-not (Test-Path -LiteralPath $full -PathType Container)) {
        Fail "缺少数据目录 $relative。模板与静态资源必须随源码一起检出，否则打包出的程序无法显示界面。"
    }
}

Write-Step "检查 Nuitka"
# --assume-yes-for-downloads 同样用于版本探测：缺少 C 编译器时 Nuitka 会在此处
# 询问是否下载 MinGW64，没有该参数会卡在交互提示上。
& $Python -m nuitka --assume-yes-for-downloads --version
if ($LASTEXITCODE -ne 0) {
    Fail ('未安装 Nuitka。请执行：uv pip install --python "' + $Python + '" nuitka')
}
$NuitkaHelp = (& $Python -m nuitka --help 2>&1 | Out-String)

# launcher.py 使用相对导入，直接作为主模块编译会失败。生成一个只调用 main()
# 的顶层入口，包内模块仍按包名正常编译。
$Entry = Join-Path $BuildDir "launcher_entry.py"
$EntrySource = @(
    "from local_sql_manager.desktop.launcher import main",
    "",
    "if __name__ == ""__main__"":",
    "    raise SystemExit(main())",
    ""
) -join [Environment]::NewLine
Set-Content -LiteralPath $Entry -Value $EntrySource -Encoding ASCII

$NuitkaArgs = @(
    "-m", "nuitka",
    "--standalone",
    "--enable-plugins=no-qt",
    "--windows-console-mode=disable",
    "--assume-yes-for-downloads",
    "--include-data-dir=local_sql_manager/templates=local_sql_manager/templates",
    "--include-data-dir=local_sql_manager/static=local_sql_manager/static",
    # uvicorn 用 import_from_string 动态加载协议/事件循环/lifespan，静态分析抓不到。
    "--include-package=uvicorn",
    "--windows-icon-from-ico=packaging/app.ico",
    "--product-name=本地多维表格",
    "--file-version=$Version",
    "--product-version=$Version",
    "--output-dir=build",
    "--output-folder-name=local_sql_manager",
    "--output-filename=local-sql-manager.exe"
)

# Nuitka 各版本对闪屏参数命名不同；只有识别到才追加，避免构建因未知参数中断。
if ($NuitkaHelp -match "--windows-splash-screen") {
    $NuitkaArgs += "--windows-splash-screen=packaging/splash.png"
} elseif ($NuitkaHelp -match "--onefile-windows-splash-screen-image") {
    Write-Host "提示：当前 Nuitka 仅在 onefile 模式支持闪屏，目录版不会显示 splash.png（文件保留，供后续单文件版本使用）。" -ForegroundColor Yellow
}

$NuitkaArgs += $Entry

Write-Step "Nuitka 编译（目录版）"
& $Python @NuitkaArgs
if ($LASTEXITCODE -ne 0) {
    Fail "Nuitka 编译失败。常见原因：缺少 C 编译器（安装 Visual Studio Build Tools 的「使用 C++ 的桌面开发」），或未安装 64 位 MinGW64。详见上方日志。"
}

if (-not (Test-Path -LiteralPath $DistDir -PathType Container)) {
    $found = Get-ChildItem -LiteralPath $BuildDir -Directory -Filter "*.dist" |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $found) {
        Fail "未找到 build 下的 *.dist 产物，Nuitka 可能未真正生成目录版。"
    }
    $DistDir = $found.FullName
}

$Exe = Join-Path $DistDir "local-sql-manager.exe"
if (-not (Test-Path -LiteralPath $Exe)) {
    Fail "目录版里没有 local-sql-manager.exe：$DistDir"
}

# 核对 Flask 的数据目录确实落到了包目录下（Flask(__name__) 默认找
# local_sql_manager/templates 与 local_sql_manager/static）。
foreach ($relative in @("local_sql_manager\templates", "local_sql_manager\static")) {
    $full = Join-Path $DistDir $relative
    $hasFile = (Test-Path -LiteralPath $full -PathType Container) -and
        [bool](Get-ChildItem -LiteralPath $full -Recurse -File | Select-Object -First 1)
    if (-not $hasFile) {
        Fail "数据目录未打进产物：$relative。请检查 Nuitka 的 --include-data-dir 目标路径是否为 local_sql_manager/...。"
    }
}

$DistBytes = (Get-ChildItem -LiteralPath $DistDir -Recurse -File |
    Measure-Object -Property Length -Sum).Sum
$DistMb = [math]::Round($DistBytes / 1MB, 1)
Write-Host ("目录版体积：{0} MB（目标 ≤ 100 MB）" -f $DistMb) -ForegroundColor Green
if ($DistMb -gt 100) {
    Write-Host "警告：目录版超过 100 MB，检查是否误打包了测试依赖或缓存。" -ForegroundColor Yellow
}

$SetupExe = Join-Path $InstallerDir "local-sql-manager-setup.exe"

if (-not $SkipInstaller) {
    Write-Step "查找 Inno Setup 6（ISCC.exe）"
    $Iscc = $null
    if ($IsccPath) {
        if (-not (Test-Path -LiteralPath $IsccPath)) {
            Fail "指定的 ISCC.exe 不存在：$IsccPath"
        }
        $Iscc = (Resolve-Path -LiteralPath $IsccPath).Path
    } else {
        $command = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
        if ($command) {
            $Iscc = $command.Source
        }
        if (-not $Iscc) {
            $candidates = @(
                (Join-Path ([Environment]::GetFolderPath("ProgramFilesX86")) "Inno Setup 6\ISCC.exe"),
                (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
                (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
            )
            foreach ($candidate in $candidates) {
                if ($candidate -and (Test-Path -LiteralPath $candidate)) {
                    $Iscc = $candidate
                    break
                }
            }
        }
    }
    if (-not $Iscc) {
        Fail "未找到 Inno Setup 6 的 ISCC.exe。请安装 Inno Setup 6（https://jrsoftware.org/isinfo.php），或用 -IsccPath 指定路径；也可以加 -SkipInstaller 只构建目录版。"
    }
    Write-Host "使用 $Iscc"

    # 官方安装包不含简体中文语言文件；存在时才启用中文安装界面。
    $ChineseLang = Join-Path (Split-Path -Parent $Iscc) "Languages\ChineseSimplified.isl"
    $IsccArgs = @(
        "/DDistDir=$DistDir",
        "/DOutputDir=$InstallerDir",
        "/DAppVersion=$Version"
    )
    if (Test-Path -LiteralPath $ChineseLang) {
        $IsccArgs += "/DChineseLang"
    }
    $IsccArgs += (Join-Path $Root "packaging\installer.iss")

    Write-Step "Inno Setup 编译安装包"
    & $Iscc @IsccArgs
    if ($LASTEXITCODE -ne 0) {
        Fail "Inno Setup 编译失败（退出码 $LASTEXITCODE）。检查 packaging\installer.iss 与上方日志。"
    }
    if (-not (Test-Path -LiteralPath $SetupExe)) {
        Fail "Inno Setup 未生成安装包：$SetupExe"
    }
    $SetupMb = [math]::Round((Get-Item -LiteralPath $SetupExe).Length / 1MB, 1)
    Write-Host ("安装包体积：{0} MB（目标 ≤ 100 MB）" -f $SetupMb) -ForegroundColor Green
}

Write-Step "构建完成"
Write-Host "目录版：$DistDir（$DistMb MB）"
if (-not $SkipInstaller) {
    Write-Host "安装包：$SetupExe"
}
