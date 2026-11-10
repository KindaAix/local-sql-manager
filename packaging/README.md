# 打包说明

把仓库打包成 Windows 目录版（Nuitka）与安装包（Inno Setup 6）。

## 前置

- Windows 10/11 x64
- Python 3.10+，仓库内已创建 `.venv`
- [Nuitka](https://nuitka.net/) 4.x（脚本按 4.3.5 验证）：`uv pip install --python .venv/Scripts/python.exe nuitka`
- Inno Setup 6：<https://jrsoftware.org/isinfo.php>（只需目录版时可不装）
- C 编译器：Visual Studio Build Tools 的「使用 C++ 的桌面开发」，或 64 位 MinGW64；
  Nuitka 带 `--assume-yes-for-downloads`，缺 MinGW64 时会尝试自动下载

先安装运行依赖与入口：

```sh
uv venv
uv pip install -e .
uv pip install nuitka
```

## 构建

```powershell
# 图标与闪屏已提交；改了样式再重新生成
python packaging/make_assets.py

# 目录版 + 安装包
powershell -ExecutionPolicy Bypass -File packaging/build.ps1

# 只做目录版
powershell -ExecutionPolicy Bypass -File packaging/build.ps1 -SkipInstaller

# 清理旧产物后重新构建
powershell -ExecutionPolicy Bypass -File packaging/build.ps1 -Clean
```

可用参数：

| 参数 | 说明 |
| --- | --- |
| `-SkipInstaller` | 只跑 Nuitka，跳过 Inno Setup |
| `-Clean` | 构建前删除整个 `build/` |
| `-IsccPath <路径>` | 指定 `ISCC.exe`；默认从 PATH 与常见安装位置探测 |
| `-Python <路径>` | 指定解释器，默认 `.venv\Scripts\python.exe` |
| `-Version 1.0.0` | exe 文件版本、产品版本与安装包版本 |

## 产物

| 路径 | 说明 |
| --- | --- |
| `build/local_sql_manager.dist/local-sql-manager.exe` | 目录版主程序，同目录包含运行时与数据文件 |
| `build/local_sql_manager.dist/local_sql_manager/templates` | Flask 模板 |
| `build/local_sql_manager.dist/local_sql_manager/static` | CSS/JS 等静态资源 |
| `build/installer/local-sql-manager-setup.exe` | Inno Setup 安装包 |

体积目标：安装包 ≤ 100 MB。脚本会打印目录版与安装包体积，目录版超 100 MB 时给出警告。
参考值：Nuitka 4.3.5 + Python 3.12 构建出的目录版约 68 MB（含网格页面模板与静态资源）。

安装包默认装到 `{autopf}\LocalSQLManager`，创建开始菜单与桌面快捷方式，安装完可选择启动。
卸载只删除安装目录，保留 `%LOCALAPPDATA%\LocalSQLManager`（配置、日志）与
`文档\LocalSQLManager`（用户库文件）。

## 数据文件怎么进包

Flask 的 `Flask(__name__, template_folder="templates", static_folder="static")` 会去
`local_sql_manager/` 包目录下找模板与静态文件，所以 Nuitka 的参数是：

```text
--include-data-dir=local_sql_manager/templates=local_sql_manager/templates
--include-data-dir=local_sql_manager/static=local_sql_manager/static
```

等号右边是相对目录版根目录（`*.dist`）的目标路径。构建完成后脚本会核对
`local_sql_manager.dist\local_sql_manager\templates` 与 `static` 非空，缺文件直接报错。
新增模板或 CSS/JS 不需要改脚本，目录整体复制。

## 图标与闪屏

`python packaging/make_assets.py` 用 Pillow 确定性生成：

- `packaging/app.ico`：16/32/48/64/128/256 六种尺寸，主色 `#3370ff`
- `packaging/splash.png`：640x360 启动图，字体优先微软雅黑，缺失时退化为无文字版本

Nuitka 4.x 的闪屏参数只支持 onefile 模式，目录版不会显示 splash；`build.ps1` 会探测
`--windows-splash-screen`，该版本没有时跳过并给出提示。安装包仍使用 `app.ico`。

## 常见失败

| 现象 | 处理 |
| --- | --- |
| `Nuitka 编译失败` 且日志提示找不到 cl.exe / gcc | 安装 Visual Studio Build Tools 的 C++ 工作负载，或安装 64 位 MinGW64 后重试 |
| `未找到 Inno Setup 6 的 ISCC.exe` | 安装 Inno Setup 6，或用 `-IsccPath` 指定，或加 `-SkipInstaller` |
| `缺少数据目录 local_sql_manager\templates` | 模板/静态资源没检出，先同步完整源码 |
| `数据目录未打进产物` | 检查 `--include-data-dir` 的目标路径是否写成 `local_sql_manager/...` |
| 安装界面是英文 | 官方 Inno Setup 不含简体中文语言文件；把 `ChineseSimplified.isl` 放进 Inno 的 `Languages` 目录后重新构建 |
| 安装时提示程序正在运行 | 应用单实例互斥体 `LocalSQLManager.SingleInstance` 生效，先从托盘退出 |

`build/`、`dist/` 已在 `.gitignore` 中，构建产物不要提交。
