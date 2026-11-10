# 本地多维表格管理器（local-sql-manager）设计文档

- 日期：2026-11-10
- 状态：待评审（第 2 稿，含评审问题处置）
- 依据：与使用者逐轮确认

## 1. 意图

把现有的 `sqlite-web` fork 从「浏览器里的 SQLite 浏览器」改造成**桌面版个人多维表格**：双击即可使用，用飞书多维表格的形态管理本地个人数据；Excel 拖进来就是一张可筛选、可分组、可编辑的表；每一次改动都能退回；同时通过 MCP 把同一批数据交给 agent 读写。

为什么现在做：现有界面是数据库工具视角——分页表格、结构页、SQL 控制台，日常记数据摩擦很大；而 agent 需要一个稳定、可审计的入口，而不是直连数据库。

## 2. 目标 / 非目标 / 反目标

**目标**
1. Excel / CSV 导入即建表，自动补辅助字段，列类型自动推断且可改。
2. 多维表格式浏览与编辑：多视图（筛选、排序、隐藏列、列序、列宽、分组 + 汇总）、单元格编辑、批量操作、粘贴、增删改字段。
3. 每一次写入可退回：操作级撤销 + 软删回收站 + 本地审计。
4. 导出 Excel；打包成 `.lsmf` 分享包，导入即得到完整状态。
5. 多表管理与表合并。
6. 进程内 MCP 服务，提供结构化增删改查工具给 agent。
7. Windows 安装包 ≤ 100MB，启动 ≤ 3 秒（目录版），托盘常驻。

**非目标（这一版不做）**
- 多用户、登录、权限、协作、评论、云同步。
- 关联字段、公式、汇总公式、看板/日历/画册视图。
- 索引、触发器、约束的管理界面（字段的增删改名**在**范围内，见 7.10）。
- 界面里的 SQL 控制台。
- 导入 `.xls` 旧格式。
- 把 Excel 追加、合并进已有表（本版导入只建新表）。
- macOS / Linux 打包。

**反目标（做成了也算失败）**
- 为了新功能牺牲「可回退」——任何绕过审计的写路径都不可接受。
- 界面上重新出现原始 SQL 入口，或把连接直接交给 agent。
- 一次性把整表读进内存来渲染或导出。
- 用户正在编辑时被 agent 悄悄覆盖。

## 3. 约束

- 仅本机使用：Web 与 MCP 都只绑 `127.0.0.1`，无登录；MCP 用 token 鉴权。
- 界面全中文，单一 Jinja2 模板体系，无前端构建步骤。
- 前端只用原生 JS + SortableJS；核心渲染留在 Jinja2 与 Python。
- 库文件是标准 `.sqlite`，可被任何 SQLite 工具打开；元数据以 `__lsm_` 前缀存在同一文件内。
- 打包：Nuitka（目录版）+ Inno Setup 安装器；闪屏用 Nuitka splash；托盘用 Windows 原生方式。

## 4. 方案取舍（已定）

| 决策点 | 备选 | 选定 | 理由 |
|---|---|---|---|
| 前端形态 | Jinja2 SSR + 轻 JS / SPA / 混合 | **SSR + 轻 JS** | 逻辑集中 Python，无构建步骤，包小；交互用局部 fetch 换 HTML 片段补足 |
| 界面骨架 | A 三层侧栏 / B 无侧栏 / C 侧栏+抽屉 | **A** | 表与视图常驻可见，切表一次点击 |
| 视图深度 | 单视图 / 多视图 / 全量 | **多视图** | 视图是纯元数据；关联与公式是另一个项目级工作量 |
| 分组 | 要+汇总 / 不要 / 只折叠 | **要 + 可选汇总** | 「按状态看分布」是多视图最常用的组合 |
| 分页 | 分页器 / 加载更多 / 触底加载 | **分页器（100/页）** | 无前端状态，刷新不丢 |
| 编辑 | 双击+键入 / 单击即编辑 / 纯键盘 | **单击选中，双击或键入编辑** | 飞书与 Excel 的共同手感 |
| 撤销 | 操作级 / 行级版本 / 快照 | **操作级 + 软删回收站** | 覆盖手滑；行级快照体积过大 |
| MCP | 进程内 HTTP / 常驻服务 / stdio | **进程内 FastMCP** | 单一服务层与审计序列 |
| 库组织 | 应用管目录 / 单库 / 文件夹即库 | **应用管目录** | 多库可切换，文件可拷走 |
| 打包 | 目录+安装器 / 单文件 / 混合 | **目录版 + Inno Setup** | 启动快，体积宽松 |
| 导入列类型 | 自动推断可改 / 全文本 / 只分两类 | **自动推断 + 预览可改** | 分组和汇总需要真实类型 |
| 导入目标 | 只建新表 / 也支持追加 | **只建新表** | 追加要列映射、upsert、部分回滚，独立一大块 |
| 并发 | 静默覆盖 / 乐观锁 / 实时推送 | **乐观锁 + 冲突即锁界面重新加载** | 防止用户把 agent 的改动写回去 |

## 5. 运行时形态

```
双击 local-sql-manager.exe
  └─ 单实例检查（命名 mutex）→ 已运行则聚焦并退出
  └─ 显示闪屏（Nuitka splash，PNG）
  └─ 读配置：%LOCALAPPDATA%\LocalSQLManager\config.json
  └─ 起 Flask（界面）+ FastMCP（agent），均绑 127.0.0.1
  └─ 端口：默认 8765，被占则选空闲端口；MCP 端口独立选择
  └─ 关闭闪屏，打开默认浏览器
  └─ 托盘常驻：打开界面 / 复制 MCP 配置 / 打开数据目录 / 打开日志 / 退出
```

- 关掉浏览器标签页不影响程序；退出只能从托盘菜单。
- 退出时先停 MCP、再停 Web、最后释放单实例锁。
- 启动失败（端口耗尽、库损坏）弹原生对话框并写日志，不静默退出。

## 6. 目录与文件

```
文档/LocalSQLManager/            # 库目录（用户可见、可备份）
  我的数据.sqlite                # 默认库
  <其他库>.sqlite
%LOCALAPPDATA%/LocalSQLManager/
  config.json                    # 库列表、端口、MCP 只读开关等
  logs/app-YYYY-MM-DD.log        # 运行日志，按天滚动，保留 14 天
```

- 启动时若检测到库目录位于 OneDrive/坚果云等同步目录下，首次运行提示同步可能引发锁冲突，并提供「切换到 `%LOCALAPPDATA%\\LocalSQLManager\\libraries`」的一键操作；设置页可随时改库目录。
- 库文件内部：

```sql
-- 每张受管表
CREATE TABLE "订单2026" (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  deleted_at TEXT,
  "订单号" TEXT, "金额" REAL, "状态" TEXT, "截止日期" TEXT
);

-- 元数据（界面隐藏）
__lsm_meta   (key TEXT PRIMARY KEY, value TEXT)         -- 格式版本
__lsm_table  (table_name TEXT PRIMARY KEY, status TEXT, created_at TEXT,
              source_file TEXT, trashed_op INTEGER)
__lsm_field  (table_name TEXT, column TEXT, type TEXT, options_json TEXT,
              position INTEGER, width INTEGER, PRIMARY KEY (table_name, column))
__lsm_view   (id INTEGER PRIMARY KEY, table_name TEXT, name TEXT,
              config_json TEXT, position INTEGER, is_default INTEGER)
__lsm_op     (id INTEGER PRIMARY KEY, ts TEXT, source TEXT, kind TEXT,
              table_name TEXT, payload_json TEXT, undone_by INTEGER,
              irreversible INTEGER DEFAULT 0)
__lsm_trash_col (op_id INTEGER, table_name TEXT, column TEXT, type TEXT,
                 row_id INTEGER, value TEXT)
```

- 逻辑字段类型：`text` / `number` / `date` / `checkbox` / `select` / `multiselect` / `url`。日期存 ISO8601，勾选存 0/1，多选存 JSON 数组。
- 视图 `config_json`：`{filters:[{column,op,value}], sorts:[{column,dir}], hidden:[], order:[], widths:{}, group:{column,collapsed}, aggregates:[{column,fn}]}`。
- 所有 DDL 与 DML 只经 `services/`，路由与 MCP 都调服务层。

## 7. 功能细节

### 7.1 导入 Excel / CSV
- 支持 `.xlsx`、`.csv`；`.xls` 提示「请另存为 xlsx」。
- 上传后解析到预览：sheet 列表（名称、列名、行数、前 5 行），空 sheet 禁用，默认全选。
- 列类型推断：整列整数/小数→数字；整列可解析日期→日期；仅 是/否/TRUE/FALSE→勾选；有前导零→文本；其余文本。单选不自动判定，预览可手动指定。可疑列（类型混杂）标黄但不阻止导入。
- 列名重复或为空自动消歧：`列名`、`列名_2`、`Column3`。
- 目标表名默认取 sheet 名，清洗非法字符；同名表提供「覆盖 / 建副本 / 跳过」。**覆盖** = 旧表重命名进「已删除表」区（可恢复），再建同名新表。
- 导入只建新表，不追加进已有表。
- 整批导入一个事务，任一步失败全部回滚；成功后写一条 `import` 审计（文件名、sheet、行数、新建表名），可整体撤销。
- 原文件不保留，只记录来源文件名。

### 7.2 网格与编辑
- 行高 34px，副表头 32px，主色 `#3370ff`，表头 `#f5f6f7`，分割线 `#dee0e3`。
- 单击选中单元格（蓝色描边）；直接键入即从该字符开始编辑；双击进入编辑；Enter 保存并下移，Tab 保存并右移，Esc 取消。
- 粘贴：解析剪贴板 TSV，从选中单元格起批量写入，一次粘贴 = 一条可撤销操作。
- 列宽拖拽、列序拖拽（SortableJS），写进当前视图配置，不改表结构。
- 批量：行首复选框 + 全选；批量删除（软删）、批量改同一字段。
- 新增行：底部「＋」追加到表末；行内不弹窗。
- 分页器：每页 100 条（可选 50/100/200），显示总条数。

### 7.3 视图
- 每张表至少一个默认视图「全部记录」，不可删除。
- 新建视图复制当前视图的筛选/排序/隐藏列；视图可重命名、复制、删除、设为默认、拖动排序。
- 筛选：多条件 AND（首版无 OR），按字段类型给操作符（文本含/等于、数字比较、日期范围、选项是/不是、勾选是/否）。
- 分组：按一个字段分组，组行可折叠，组头显示条数与 0–3 个汇总（求和/计数/平均/最小/最大）。
- **分组与分页的组合语义**：仍按行分页；组头显示该组在整个筛选集下的总条数与汇总（单独一条 `GROUP BY` 查询），组跨页时组头标「（续）」。页脚同时显示「第 1–100 行 / 共 N 行」。

### 7.4 撤销与历史
- 每次写入生成 `__lsm_op`，`kind` 覆盖：`insert` / `update` / `delete` / `restore` / `purge`（行级彻底删除）/ `import` / `merge` / `undo` / `create_table` / `rename_table` / `drop_table` / `restore_table` / `purge_table`（表级彻底删除）/ `create_field` / `rename_field` / `drop_field` / `view`。
- `payload_json` 记受影响行 id 与前后值；表级操作记表名；删列把整列值写入 `__lsm_trash_col` 以便撤销回填。
- 撤销 = 执行相反操作，并写一条指向原操作的 `undo` 记录；撤销可被撤销。
- 入口两处：右下角 toast「已删除 3 条 · 撤销」（仅最近一次）；侧栏「操作历史」页（整库/按表筛选，逐条回退）。
- 批量与导入的回退只触及该操作记录的行/表，不影响之后的数据。**撤销导入** = 把导入建的表移入「已删除表」区；若该表在此之后有其它写操作，拒绝并提示先撤销后续操作。**撤销合并** 同理。
- `purge`（彻底删除）记审计但标 `irreversible=1`，不提供撤销，需二次确认。

### 7.5 回收站
- 删除 = 写 `deleted_at`，所有视图默认过滤 `deleted_at IS NULL`。
- 回收站按表列出已删行：可恢复（写 `restore`）或彻底删除（`purge`）。
- 「已删除表」区：删表时重命名为 `__lsm_trash_<op>_<name>` 留在库内，可恢复（重名自动加后缀）或彻底删除。

### 7.6 多表与合并
- 侧栏按库列出表；表可重命名、复制、删除（进「已删除表」区，可恢复）。
- 合并：选 2 张以上受管表 → 确认页（目标表名、列映射、冲突预览）→ 生成新表，源表不动。
- 列对齐按名称（忽略大小写与首尾空格）；同名列类型冲突以目标类型为准并报告受影响单元格数；缺失列补空。
- 合并写一条 `merge` 审计；撤销即把新表移入「已删除表」区，若新表已被改动则拒绝并提示。

### 7.7 外部表（非本应用建的表）
- 打开任意 `.sqlite` 后，未登记在 `__lsm_table` 的表只读呈现，表头显示「启用编辑」。
- 前置校验：
  - 已有单列整数主键 → 直接复用，不新增 `id`。
  - 无主键 → 重建时加 `id INTEGER PRIMARY KEY AUTOINCREMENT`。
  - 已有名为 `id` 但非整型/非主键的列 → 拒绝启用，提示先重命名。
  - 复合主键 → 拒绝启用，保持只读。
- 启用编辑即按 12 步重建表，补 `id`、`created_at`、`updated_at`、`deleted_at`，保留数据与索引；原 UNIQUE 索引改写为部分索引 `WHERE deleted_at IS NULL`，确认页明确说明「唯一性只在未删除行之间成立」。重建前后各存表快照，失败自动回滚。

### 7.8 导出与分享
- 导出 Excel：当前视图（筛选/排序/可见列）/ 整表 / 多表合成一个工作簿（多 sheet）。
- CSV 保留，列选择沿用旧逻辑。
- 分享 `.lsmf`：zip 内 `manifest.json` + 库文件；manifest 含格式版本、应用版本、导出时间、表清单与行数。
- 导入 `.lsmf`：校验 manifest 版本不高于当前、zip 只有预期条目、无路径穿越、解压大小与压缩比在阈值内；解压到库目录临时文件 → 原子改名 → 作为**新库**打开（不合并），同名自动加 `-2`。

### 7.9 MCP
- 进程内 FastMCP（streamable-http），绑 `127.0.0.1`，随机 token；托盘「复制 MCP 配置」输出可直接粘贴的 JSON。
- 只暴露结构化工具，不提供任意 SQL：
  `list_libraries` / `list_tables` / `get_schema` / `list_views` / `get_rows` / `count_rows` / `create_rows` / `update_rows` / `delete_rows` / `restore_rows` / `create_table` / `add_field` / `merge_tables` / `get_history` / `undo`。
- 全部走同一服务层，同样写审计、同样可撤销；设置里可把 MCP 切为只读。

### 7.10 字段管理
- 增列：选类型（含单选/多选的选项列表）、列名、插入位置；`ALTER TABLE ADD COLUMN`，已有行该列为空。
- 改名：`ALTER TABLE RENAME COLUMN`，同步 `__lsm_field` 与所有视图配置里的列引用。
- 删除列：确认后把整列值存入 `__lsm_trash_col`，再重建表去掉该列；撤销时重建列并回填。
- 改类型：只改 `__lsm_field.type`，不重建物理列；读取、筛选、排序、汇总按逻辑类型 `CAST`；无法转换的值原样保留，界面标黄提示。
- 以上全部写审计、可撤销；列顺序与列宽属于视图配置，不入审计。

### 7.11 并发与冲突
- 每次保存带上该行读到的 `updated_at`；服务端比对不一致即返回 409 与最新行数据。
- 前端收到 409 立刻弹提示「该记录已被修改，正在重新加载」，给整个网格加遮罩、禁用所有写入入口（单元格、批量、新增、粘贴、拖拽），重新拉取当前页后解除。
- 拉取失败保持锁定并提供「重试」；用户不得在锁定状态下继续编辑。
- 不做推送、不做实时同步；其余刷新发生在翻页、切换视图或手动刷新时。
- MCP 写入同样更新 `updated_at`，因此 agent 的改动会触发上面的保护。

## 8. 代码结构

```
local_sql_manager/
  app.py                  # Flask 工厂、蓝图注册、错误页
  routes/
    libraries.py  tables.py  grid.py  views.py
    import_export.py  history.py  share.py  settings.py
  services/
    library.py  schema.py  rows.py  views.py  undo.py
    import_excel.py  export_excel.py  share.py  audit.py  types.py
  mcp_server.py           # FastMCP 实例与工具（只调 services）
  desktop/
    launcher.py  tray.py  splash.py  config.py  logging_setup.py
  templates/  static/
tests/
```

- 删除旧的 `sqlite_web/sqlite_web.py`、`executor.py`；`wrap` 的分页/排序思路并入 `services/rows.py`；移除 Pygments 与 b64 行 token（改用整数 `id`）。
- 前端只保留 `static/js/grid.js`（选中、编辑、粘贴、拖拽、toast、冲突锁）与 `views.js`（视图配置面板）；样式手写，替换 Bootstrap。

## 9. 性能与容量

- 导入完成后自动为每个业务列建普通索引（名称 `__lsm_idx_<表名长度>_<表名>_<列名长度>_<列名>`，长度前缀保证跨表不撞名；界面不可见）；合并、增列、外部表重建时同样补建——这是 300ms 目标成立的前提。索引名只由 `schema.ensure_indexes` / `schema._index_name` 生成，其他模块不得自行拼接。
- 单表 10 万行内：首页加载 < 300ms（按 `id` 分页，筛选/排序走索引）。
- 导入 5 万行 Excel < 30s，进度分阶段（解析 / 建表 / 写入）。
- 导出 10 万行 < 20s，流式写出。
- 分组时额外一条 `GROUP BY` 聚合查询；10 万行内开销可控，超出提示改用筛选。
- 启动到浏览器可交互 < 3s（目录版）；安装包 ≤ 100MB（预估 40–60MB）。

## 10. 测试

- 服务层单测（内存 SQLite）：类型推断、筛选/排序/分组 SQL、撤销正反向、软删与恢复、删列回填、合并列对齐、外部表重建（含 UNIQUE 改写、主键复用、拒绝分支）、lsmf 校验失败分支、并发 409。
- 应用层测试（`test_client`）：导入预览 → 确认 → 网格渲染 → 保存 → 撤销 → 回收站恢复 的完整链路。
- MCP 工具测试：直接调用工具函数，断言与界面路径产生相同审计。
- 保留现有 `tests.py` 中仍适用的断言，删掉被移除功能的用例。

## 11. 风险与对策

| 风险 | 对策 |
|---|---|
| 同进程跑 WSGI(Flask) 与 ASGI(FastMCP) | 两线程各自 serve，共用服务层写锁，写操作串行 |
| Nuitka 打包 pydantic/starlette/uvicorn 体积与兼容 | 先做最小打包验证；超标改用更轻的 MCP 传输 |
| 大 Excel 解析占内存 | openpyxl 只读模式 + 流式写入；超阈值提示拆分 |
| 12 步重建外部表出错 | 事务 + 重建前后快照 + 失败回滚，界面提示影响行数 |
| 用户把「关闭标签页」当成退出 | 托盘菜单是唯一退出入口，界面提供「退出程序」 |
| OneDrive 同步目录下 SQLite 锁冲突 | 启动检测并提示切换目录 |

## 12. 留给实现者的决定

- Python 包布局与命名（文档给的是建议）。
- CSS 细节数值与图标来源（内联 SVG）。
- 导入进度用 SSE 还是轮询。
- `.lsmf` 压缩级别与阈值具体数值。
- 托盘库选型（`pystray` vs ctypes），Inno Setup 安装目录与卸载行为。
- MCP token 的存储与展示形式。
- 索引的命名与重建时机细化。
