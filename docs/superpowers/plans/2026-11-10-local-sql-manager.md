# 本地多维表格管理器 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有仓库内新建 `local_sql_manager` 包，交付一个 Windows 桌面版个人多维表格：Excel 导入建表、多视图网格、操作级撤销、`.lsmf` 分享、进程内 MCP。

**Architecture:** Flask + Jinja2 服务端渲染，写入走统一的 `services/` 层（审计与撤销在此层），MCP 工具与 Web 路由都只调服务层；SQLite 用标准库 `sqlite3`（去掉 peewee），桌面壳（单实例、托盘、闪屏、开浏览器）在 `desktop/`，与 Web、MCP 同进程。

**Tech Stack:** Python 3.10+、Flask、openpyxl、FastMCP、pystray + Pillow、原生 JS + SortableJS、Nuitka + Inno Setup。

**Spec:** `docs/superpowers/specs/2026-11-10-local-sql-manager-design.md`

## Global Constraints

- 运行时依赖只允许：`flask`、`openpyxl`、`fastmcp`、`pystray`、`pillow`；不引入 peewee、pygments、前端框架与构建步骤。
- 所有内部表以 `__lsm_` 开头；用户表与列名在 SQL 中一律用双引号转义。
- 时间统一存 UTC ISO8601（形如 `2026-11-10T08:00:00Z`），界面渲染为本地时间。
- 每个写操作必须先 `audit.record(...)` 再返回；任何绕过审计的写路径视为缺陷。
- 分页默认每页 100 行；行高 34px；主色 `#3370ff`；表头 `#f5f6f7`；分割线 `#dee0e3`。
- 服务只绑 `127.0.0.1`；界面文案全中文。
- 安装包 ≤ 100MB；目录版启动到可交互 ≤ 3 秒。
- 测试命令：全量 `python -m unittest discover -s tests -v`，单文件 `python -m unittest tests.test_x -v`。

## Review Focus

1. 外部表的异常主键（TEXT 型 `id`、复合主键）——必须明确拒绝启用编辑，绝不能静默重建坏数据（Task 14）。
2. 软删后的唯一值：部分索引下恢复一行可能与现存行冲突，应给出明确错误而不是 500（Task 14、Task 8）。
3. Excel 脏输入：混合类型、前导零、重复/空列名、超长 sheet 名、全空 sheet（Task 15）。
4. agent 与界面并发写：保存必须命中 409 并锁住界面，不能静默覆盖（Task 8、Task 20）。
5. 撤销链上的目标已被后续修改：导入/合并/删表/删列的撤销必须拒绝并说明，而不是强行执行（Task 10、Task 13、Task 18）。
6. `.lsmf` 恶意或损坏包：路径穿越、zip bomb、版本超前、缺 manifest（Task 17）。

---

## 并行执行方案（团队分工）

**规模判断：** 25 个任务、约 6 个模块；核心依赖是 `services/models.py` 的签名。按 4 条流 + 1 名集成人拆，瓶颈在波次 0 之后的数据层。

### 分层并行（v2）

按依赖切成 8 层，层内并行、层间串行；每层合并后打 tag，下一层从 tag 拉 worktree。


```
层 0（已完成）  1 骨架 → 2 模型 → 3 库 → 4 类型                    tag contracts-v1
层 1（4 并行）  A 5 建表 → 6 查询 → 7 分页   B 9 审计   C 23 桌面壳   D 17 lsmf
层 2（3 并行）  A 8 写入/冲突   B 11 视图   C 13 表级与回收站
层 3（3 并行）  A 10 撤销   B 14 外部表   C 15 Excel 导入
层 4（2 并行）  A 12 字段管理   B 16 导出
层 5（3 并行）  A 18 合并   B 19 网格页   C 22 MCP
层 6（2 并行）  A 20 写入 API + JS   B 21 导入/导出/分享/视图/历史页
层 7（串行）    24 打包 → 25 清理

```

**分层依据（同层文件必须互斥）**

| 层 | 任务 | 独占文件 | 依赖 |
|---|---|---|---|
| 1 | 5, 6, 7 | `services/schema.py`、`query.py`、`rows.py`（只读部分） | 3, 4 |
| 1 | 9 | `services/audit.py` | 2, 3 |
| 1 | 23 | `desktop/*`、`pyproject.toml`（入口） | 1 |
| 1 | 17 | `services/share.py` | 1（只用 zip + sqlite3） |
| 2 | 8 | `services/rows.py`（写入部分） | 7, 9 |
| 2 | 11 | `services/views.py` | 5 |
| 2 | 13 | `services/schema.py`（表级）、`services/undo.py` 注册表接口 | 5, 9 |
| 3 | 10 | `services/undo.py`（核心 + 行级处理器） | 8 |
| 3 | 14 | `services/adopt.py` | 5, 8 |
| 3 | 15 | `services/import_excel.py` | 5, 13 |
| 4 | 12 | `services/schema.py`（字段级） | 5, 10, 11 |
| 4 | 16 | `services/export_excel.py` | 7, 11 |
| 5 | 18 | `services/merge.py`、`services/undo.py`（merge 处理器） | 5, 9, 10 |
| 5 | 19 | `routes/grid.py`、`templates/*`、`static/css/*` | 7, 11 |
| 5 | 22 | `mcp_server.py` | 5, 7, 8, 9, 10, 11 |
| 6 | 20 | `routes/grid_api.py`、`static/js/*` | 8, 9, 19 |
| 6 | 21 | `routes/import_export.py`、`views.py`、`history.py`、其余模板 | 10, 11, 14, 15, 16, 17, 18 |
| 7 | 24, 25 | `packaging/*`、删除旧代码 | 全部 |

**并发上限 4。** 同一层里若两个任务都要改同一个文件，说明分层错了：把其中一个推到下一层，不要靠"先到先得"。

**Worktree 机制**


```bash
# 每层开工时，每个成员一条独立分支与工作树
git worktree add .worktrees/L1a -b layer1/t5-7 contracts-v1
git worktree add .worktrees/L1b -b layer1/audit contracts-v1
# 成员在自己目录里跑测试，共享同一份 .venv
cd .worktrees/L1a && D:/Projects/local-sql-manager/.venv/Scripts/python.exe -m unittest discover -s tests -t . -v
# 合并（按依赖顺序，冲突由集成人解）
git merge --no-ff layer1/t5-7 && git tag layer-1
rm -rf .worktrees/L1a && git worktree prune

```

- `.worktrees/` 加入 `.gitignore`，不提交。
- 每个成员**只在自己的 worktree 提交**，不碰主工作树。
- 合并后立刻跑全量测试并打 `layer-N` tag；红则回退该分支，不进入下一层。
- 静态资源（templates/static）在层 5、6 由不同成员独占，层内不并行改同一模板。

### 文件归属（避免合并冲突）

- 每条流只创建自己清单里的文件；跨流文件只有四个：`app.py`、`pyproject.toml`、`templates/base.html`、`tests/__init__.py`。
- 蓝图注册约定：每条流在 `routes/<name>.py` 顶部暴露 `bp = Blueprint(...)`；`app.py` 的注册行由**集成人**在合并时补，流内开发用自建的最小 app 跑测试。
- 测试文件一名一文件（`tests/test_<area>.py`），不共享测试基类文件；需要的 fixture 写在自己的测试文件里，重复优于冲突。
- 数据库 schema 只有 Task 3 能改；其他流需要新列时提给集成人，不自行 `ALTER`。
- `models.py` 冻结后只做**增量**修改（新增字段/异常类），不改已有字段名与顺序。

### 合并顺序与门禁

1. 层 0 已合并到 `feature/sunxd/refactor`，打 tag `contracts-v1`。
2. 每层开工时从当前 HEAD 拉 worktree 与分支（`git worktree add .worktrees/L2a -b layer2/write HEAD`），成员只在自己 worktree 内提交。
3. 层内分支按独占文件互斥，合并顺序无所谓；每次合并后跑全量 `python -m unittest discover -s tests -t . -v` 并打 `layer-N` tag。
4. 冲突由集成人解决；解决后必须重跑全量，红则回退该分支并打回成员。
5. 层 6 的 Task 20 含前端交互，无自动化测试，合并前按 Task 20 Step 5 的人工清单走一遍。

### 并行时的纪律

- 任何跨流接口缺口，先在 `models.py` 或对应 service 里补签名并提交，再各自实现；不允许在自己流里私造一个同名函数。
- 每个任务结束就提交，不攒大 commit；提交信息沿用 `feat: ...`。
- 遇到需要改契约的情况，停下来通知集成人，由集成人统一改并广播。

---

### Task 1: 项目骨架与健康检查

**Files:**
- Create: `pyproject.toml`（替换现有内容）、`local_sql_manager/__init__.py`、`local_sql_manager/app.py`、`tests/__init__.py`、`tests/test_app_factory.py`

**Interfaces:**
- Produces: `create_app() -> flask.Flask`；`GET /healthz` 返回 `ok`。

- [ ] **Step 1: 写失败测试**


```python
# tests/test_app_factory.py
import unittest
from local_sql_manager.app import create_app

class TestAppFactory(unittest.TestCase):
    
def test_healthz(self):
        client = create_app().test_client()
        r = client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_data(as_text=True), "ok")

```

- [ ] **Step 2: 运行确认失败**

Run: `python -m unittest tests.test_app_factory -v` — 期望 ModuleNotFoundError。

- [ ] **Step 3: 实现**

`pyproject.toml`：`name = "local-sql-manager"`，`requires-python = ">=3.10"`，依赖按 Global Constraints；`[project.scripts] local-sql-manager = "local_sql_manager.desktop.launcher:main"`（Task 23 前指向一个占位函数）。`create_app()` 建 Flask 实例并注册一条 `/healthz` 路由，模板与静态目录指向包内。

- [ ] **Step 4: 运行确认通过**

Run: `python -m unittest tests.test_app_factory -v` — 期望 PASS。

- [ ] **Step 5: 提交**


```bash
git add pyproject.toml local_sql_manager tests
git commit -m "feat: 新建 local_sql_manager 包与健康检查"

```

### Task 2: 数据模型与时间工具

**Files:**
- Create: `local_sql_manager/services/__init__.py`、`services/models.py`、`services/util.py`、`tests/test_models.py`

**Interfaces:**
- Produces: `Field(name, type, options=(), position=0, width=140)`、`Group(key, count, aggregates)`、`PageResult(rows, total, groups=(), page=1, page_size=100)`、`ViewConfig(filters=(), sorts=(), hidden=(), order=(), widths={}, group=None, aggregates=())`、`ConflictError(rows)`、`StaleOperation`、`Irreversible`、`ShareError`、`AdoptError`、`UnsupportedFormat`；`utcnow_iso() -> str`。

- [ ] **Step 1: 写失败测试**


```python
class TestUtil(unittest.TestCase):
    
def test_utcnow_format(self):
        self.assertRegex(utcnow_iso(), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

class TestViewConfig(unittest.TestCase):
    
def test_defaults_are_independent(self):
        a, b = ViewConfig(), ViewConfig()
        a.filters.append({"column": "x"})
        self.assertEqual(b.filters, [])

```

- [ ] **Step 2: 运行确认失败** — `python -m unittest tests.test_models -v`。

- [ ] **Step 3: 实现** — 用 `@dataclass`；可变默认一律 `field(default_factory=...)`；异常类均为普通 `Exception` 子类，`ConflictError.rows` 存冲突行的最新值。

- [ ] **Step 4: 运行确认通过** — 同上命令，期望 PASS。

- [ ] **Step 5: 提交** — `git commit -m "feat: 数据模型与时间工具"`。

### Task 3: 打开库与元数据初始化

**Files:**
- Create: `services/library.py`、`tests/test_library.py`

**Interfaces:**
- Produces: `META_VERSION = 1`；`open_library(path) -> Library`（`Library.conn`、`Library.path`，`conn.row_factory = sqlite3.Row`、`PRAGMA foreign_keys=ON`）；`ensure_meta(conn) -> None`；表 `__lsm_meta/__lsm_table/__lsm_field/__lsm_view/__lsm_op/__lsm_trash_col` 按 spec §6 建立。

- [ ] **Step 1: 写失败测试**


```python
def test_creates_meta_tables(tmp_db):
    lib = open_library(tmp_db)
    names = {r[0] for r in lib.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    self.assertTrue({"__lsm_meta", "__lsm_field", "__lsm_view", "__lsm_op"} <= names)
    self.assertEqual(meta_version(lib.conn), 1)

def test_rejects_newer_format(tmp_db):
    open_library(tmp_db).conn.execute("UPDATE __lsm_meta SET value='9' WHERE key='version'")
    with self.assertRaises(UnsupportedFormat):
        open_library(tmp_db)

```

- [ ] **Step 2: 运行确认失败** — `python -m unittest tests.test_library -v`。

- [ ] **Step 3: 实现** — 建表语句照 spec §6；写入 `('version', '1')`；读到的版本大于 `META_VERSION` 时抛 `UnsupportedFormat`。

- [ ] **Step 4: 运行确认通过**。

- [ ] **Step 5: 提交** — `git commit -m "feat: 库打开与元数据初始化"`。

### Task 4: 逻辑类型与推断

**Files:**
- Create: `services/types.py`、`tests/test_types.py`

**Interfaces:**
- Produces: `TYPES`；`infer_type(values: Sequence[str]) -> tuple[str, bool]`（返回类型与「可疑」标记）；`cast_expr(column: str, type_: str) -> str`；`coerce(value, type_) -> object`。

- [ ] **Step 1: 写失败测试**


```python
def test_leading_zero_is_text(self):
    self.assertEqual(infer_type(["00123", "00456"]), ("text", False))

def test_numbers_and_suspicious(self):
    self.assertEqual(infer_type(["1", "2", "3"]), ("number", False))
    self.assertEqual(infer_type(["1", "2", "abc"]), ("text", True))

def test_dates_and_checkbox(self):
    self.assertEqual(infer_type(["2026-01-02", "2026-03-04"])[0], "date")
    self.assertEqual(infer_type(["是", "否", "是"])[0], "checkbox")

def test_cast_expr(self):
    self.assertEqual(cast_expr("金额", "number"), 'CAST("金额" AS REAL)')

```

- [ ] **Step 2: 运行确认失败**。

- [ ] **Step 3: 实现** — 规则按 spec §7.1；空字符串不计入推断；全列解析失败但非空时返回 `("text", True)`。

- [ ] **Step 4: 运行确认通过**。

- [ ] **Step 5: 提交** — `git commit -m "feat: 字段类型推断与 CAST"`。

### Task 5: 建表与自动索引

**Files:**
- Create: `services/schema.py`、`tests/test_schema.py`

**Interfaces:**
- Consumes: `Field`、`types.cast_expr`。
- Produces: `ColumnSpec(name, type, options=())`；`create_table(conn, name, columns, source_file=None) -> str`；`list_tables(conn) -> list[str]`；`get_fields(conn, table) -> list[Field]`；`ensure_indexes(conn, table) -> None`；`rename_table/drop_table/restore_table`（Task 13 使用，此处只建前四项）。

- [ ] **Step 1: 写失败测试**


```python
def test_create_table_adds_aux_columns(conn):
    create_table(conn, "订单", [ColumnSpec("金额", "number")])
    cols = [r["name"] for r in conn.execute('PRAGMA table_info("订单")')]
    self.assertEqual(cols[:4], ["id", "created_at", "updated_at", "deleted_at"])
    self.assertIn("金额", cols)

def test_registers_field_metadata(conn):
    create_table(conn, "订单", [ColumnSpec("金额", "number"), ColumnSpec("状态", "select", ("新建", "完成"))])
    fields = get_fields(conn, "订单")
    self.assertEqual(fields[1].type, "select")
    self.assertEqual(fields[1].options, ("新建", "完成"))

def test_creates_index_per_business_column(conn):
    create_table(conn, "订单", [ColumnSpec("金额", "number")])
    idx = [r[0] for r in conn.execute('PRAGMA index_list("订单")')]
    self.assertIn("__lsm_idx_2_订单_2_金额", idx)

```

- [ ] **Step 2: 运行确认失败**。

- [ ] **Step 3: 实现** — 物理列类型：number→REAL、checkbox→INTEGER、其余 TEXT；`__lsm_table` 记 `status='active'`；索引名做 SQL 标识符转义。

- [ ] **Step 4: 运行确认通过**。

- [ ] **Step 5: 提交** — `git commit -m "feat: 建表、字段元数据与自动索引"`。

### Task 6: 筛选与排序 SQL

**Files:**
- Create: `services/query.py`、`tests/test_query.py`

**Interfaces:**
- Produces: `OPS: dict[str, tuple[str, ...]]`；`build_where(filters, fields) -> tuple[str, list]`；`build_order(sorts, group) -> str`；`page_sql(table, fields, view, limit, offset) -> tuple[str, list]`。

- [ ] **Step 1: 写失败测试**


```python
def test_text_filter_uses_param(self):
    sql, params = build_where([{"column": "客户", "op": "contains", "value": "云栖"}],
                              [Field("客户", "text")])
    self.assertIn("LIKE ?", sql)
    self.assertEqual(params, ["%云栖%"])

def test_number_filter_casts(self):
    sql, params = build_where([{"column": "金额", "op": "gte", "value": "100"}],
                              [Field("金额", "number")])
    self.assertIn('CAST("金额" AS REAL) >= ?', sql)

def test_unknown_column_ignored(self):
    self.assertEqual(build_where([{"column": "不存在", "op": "eq", "value": 1}], []), ("", []))

```

- [ ] **Step 2: 运行确认失败**。

- [ ] **Step 3: 实现** — 参数一律绑定，不拼字面值；日期比较用 `substr` 取日期部分；空条件返回空串。

- [ ] **Step 4: 运行确认通过**。

- [ ] **Step 5: 提交** — `git commit -m "feat: 筛选与排序 SQL 构建"`。

### Task 7: 分页读取与分组汇总

**Files:**
- Create: `services/rows.py`（本任务只加 `get_page`）、`tests/test_rows_read.py`

**Interfaces:**
- Consumes: `query.page_sql`、`PageResult`、`Group`。
- Produces: `get_page(conn, table, view, page=1, page_size=100) -> PageResult`。

- [ ] **Step 1: 写失败测试**


```python
def test_excludes_soft_deleted_and_paginates(seeded):
    conn = seeded  # 3 行，其中 1 行 deleted_at 非空
    page = get_page(conn, "订单", ViewConfig(), page=1, page_size=1)
    self.assertEqual(len(page.rows), 1)
    self.assertEqual(page.total, 2)

def test_group_counts_whole_filter_set(seeded):
    view = ViewConfig(group={"column": "状态"})
    page = get_page(seeded_6_rows, "订单", view, page=1, page_size=2)
    counts = {g.key: g.count for g in page.groups}
    self.assertEqual(counts["完成"], 4)

```

- [ ] **Step 2: 运行确认失败**。

- [ ] **Step 3: 实现** — 单页查询 + 一条 `COUNT(*)`；有分组时再跑一条 `GROUP BY` 聚合（汇总 `SUM/COUNT/AVG/MIN/MAX`），组跨页只影响行，不影响组头数值。

- [ ] **Step 4: 运行确认通过**。

- [ ] **Step 5: 提交** — `git commit -m "feat: 分页读取与分组汇总"`。

### Task 8: 写入与并发冲突

**Files:**
- Modify: `services/rows.py`、`tests/test_rows_write.py`

**Interfaces:**
- **实现顺序：本任务依赖 Task 9（审计），S1 流里先做 9 再做 8。**
- Produces: `insert_rows(conn, table, rows, source="web") -> int`；`update_cells(conn, table, changes: dict[int, dict], expected: dict[int, str], source="web") -> int`；`soft_delete(conn, table, ids, source="web") -> int`；`restore_rows(...)`；`purge_rows(...)`。以上均返回 `__lsm_op.id`，内部先 `audit.record`。`update_cells` 在 `updated_at` 不匹配时抛 `ConflictError(rows=[最新行 dict])`。

- [ ] **Step 1: 写失败测试**


```python
def test_update_bumps_updated_at_and_records_op(conn, seeded):
    op = update_cells(conn, "订单", {1: {"金额": 99}}, {1: seeded_updated_at(conn, 1)})
    self.assertEqual(conn.execute('SELECT "金额" FROM "订单" WHERE id=1').fetchone()[0], 99)
    self.assertEqual(get_op(conn, op)["kind"], "update")

def test_stale_updated_at_conflicts(conn, seeded):
    with self.assertRaises(ConflictError) as ctx:
        update_cells(conn, "订单", {1: {"金额": 1}}, {1: "2000-01-01T00:00:00Z"})
    self.assertEqual(ctx.exception.rows[0]["id"], 1)

def test_soft_delete_is_invisible_to_page(conn, seeded):
    soft_delete(conn, "订单", [1])
    self.assertEqual(get_page(conn, "订单", ViewConfig()).total, len(seeded) - 1)

```

- [ ] **Step 2: 运行确认失败**。

- [ ] **Step 3: 实现** — 每个写方法在单个事务内完成「读旧值 → 写 → 记 op」，op payload 存 `{"rows": [{"id", "before", "after"}]}`。

- [ ] **Step 4: 运行确认通过**。

- [ ] **Step 5: 提交** — `git commit -m "feat: 写入、软删与乐观锁冲突"`。

### Task 9: 审计记录

**Files:**
- Create: `services/audit.py`、`tests/test_audit.py`

**Interfaces:**
- Produces: `record(conn, kind, table, source, payload) -> int`；`get_op(conn, op_id) -> dict`；`history(conn, table=None, limit=50) -> list[dict]`；`mark_undone(conn, op_id, undo_id) -> None`。

- [ ] **Step 1: 写失败测试**


```python
def test_record_and_history(conn):
    op = record(conn, "update", "订单", "web", {"rows": []})
    self.assertEqual(get_op(conn, op)["source"], "web")
    self.assertEqual(history(conn, "订单")[0]["id"], op)

def test_soft_delete_flag_survives(conn):
    op = record(conn, "purge", "订单", "mcp", {}, irreversible=True)
    self.assertEqual(get_op(conn, op)["irreversible"], 1)

```

- [ ] **Step 2: 运行确认失败**。

- [ ] **Step 3: 实现** — `payload` 存 JSON 文本；`history` 按 `ts DESC, id DESC`；`mark_undone` 同时写 `undone_by`。

- [ ] **Step 4: 运行确认通过**。

- [ ] **Step 5: 提交** — `git commit -m "feat: 审计记录与历史查询"`。

### Task 10: 撤销核心

**Files:**
- Create: `services/undo.py`、`tests/test_undo.py`

**Interfaces:**
- Consumes: `audit.get_op`、`rows.*`。
- Produces: `undo(conn, op_id, source="web") -> int`；对 `insert/update/delete/restore` 生效；对 `irreversible` 抛 `Irreversible`；不支持或目标已变的 kind 抛 `StaleOperation`。

- [ ] **Step 1: 写失败测试**


```python
def test_undo_update_restores_previous_value(conn, seeded):
    op = update_cells(conn, "订单", {1: {"金额": 500}}, {1: updated_at(conn, 1)})
    undo(conn, op)
    self.assertNotEqual(conn.execute('SELECT "金额" FROM "订单" WHERE id=1').fetchone()[0], 500)

def test_undo_of_undo(conn, seeded):
    op = update_cells(conn, "订单", {1: {"金额": 500}}, {1: updated_at(conn, 1)})
    u1 = undo(conn, op)
    u2 = undo(conn, u1)
    self.assertEqual(get_op(conn, u2)["kind"], "undo")

def test_purge_is_irreversible(conn):
    op = record(conn, "purge", "订单", "web", {}, irreversible=True)
    with self.assertRaises(Irreversible):
        undo(conn, op)

```

- [ ] **Step 2: 运行确认失败**。

- [ ] **Step 3: 实现** — 按 `kind` 分派到处理器；行级撤销校验目标行仍存在（否则 `StaleOperation`）；撤销本身写一条 `undo` op，payload 指回原 op。

- [ ] **Step 4: 运行确认通过**。

- [ ] **Step 5: 提交** — `git commit -m "feat: 操作级撤销"`。

### Task 11: 视图 CRUD

**Files:**
- Create: `services/views.py`、`tests/test_views.py`

**Interfaces:**
- Produces: `default_view(table) -> dict`（`{"name": "全部记录", "config": ViewConfig()}`）；`list_views(conn, table)`；`create_view(conn, table, name, config) -> int`；`update_view(conn, view_id, config) -> None`；`delete_view(conn, view_id)`；`set_default(conn, table, view_id)`；`load_config(conn, view_id) -> ViewConfig`。

- [ ] **Step 1: 写失败测试**


```python
def test_creating_table_makes_default_view(conn):
    create_table(conn, "订单", [ColumnSpec("金额", "number")])
    views = list_views(conn, "订单")
    self.assertEqual(len(views), 1)
    self.assertTrue(views[0]["is_default"])

def test_default_view_cannot_be_deleted(conn, table_with_default):
    with self.assertRaises(ValueError):
        delete_view(conn, table_with_default)

def test_update_prunes_unknown_columns(conn, table_with_default):
    update_view(conn, table_with_default, ViewConfig(hidden=["不存在", "金额"]))
    self.assertEqual(load_config(conn, table_with_default).hidden, ["金额"])

```

- [ ] **Step 2: 运行确认失败**。

- [ ] **Step 3: 实现** — `config` 序列化为 `config_json`；保存前用 `get_fields` 过滤未知列；删除默认视图抛 `ValueError`。

- [ ] **Step 4: 运行确认通过**。

- [ ] **Step 5: 提交** — `git commit -m "feat: 视图配置存取"`。

### Task 12: 字段管理

**Files:**
- Modify: `services/schema.py`、`services/undo.py`、`tests/test_fields.py`

**Interfaces:**
- Produces: `add_field(conn, table, spec, after=None) -> int`；`rename_field(conn, table, old, new) -> int`；`drop_field(conn, table, name) -> int`；`set_field_type(conn, table, name, type_, options=()) -> int`。

- [ ] **Step 1: 写失败测试**


```python
def test_add_field_is_empty_for_existing_rows(conn, seeded):
    add_field(conn, "订单", ColumnSpec("备注", "text"))
    self.assertIsNone(conn.execute('SELECT "备注" FROM "订单" WHERE id=1').fetchone()[0])

def test_rename_field_updates_view_config(conn, seeded_with_view):
    conn, view_id = seeded_with_view
    rename_field(conn, "订单", "金额", "金额元")
    self.assertEqual(load_config(conn, view_id).sorts[0]["column"], "金额元")

def test_drop_field_backs_up_values_and_undo_restores(conn, seeded):
    op = drop_field(conn, "订单", "金额")
    undo(conn, op)
    self.assertEqual(conn.execute('SELECT "金额" FROM "订单" WHERE id=1').fetchone()[0], 100)

```

- [ ] **Step 2: 运行确认失败**。

- [ ] **Step 3: 实现** — 删列前把 `(row_id, value)` 写入 `__lsm_trash_col` 并重建表；撤销时重建列并回填；改类型只改 `__lsm_field`。

- [ ] **Step 4: 运行确认通过**。

- [ ] **Step 5: 提交** — `git commit -m "feat: 字段增删改"`。

### Task 13: 表级操作与回收站

**Files:**
- Modify: `services/schema.py`、`services/undo.py`、`tests/test_tables.py`

**Interfaces:**
- Produces: `rename_table(conn, old, new) -> int`；`drop_table(conn, table) -> int`；`restore_table(conn, table) -> int`；`list_trashed_tables(conn)`；`purge_table(conn, table) -> int`（`irreversible`）。

- [ ] **Step 1: 写失败测试**


```python
def test_drop_renames_into_trash(conn, seeded):
    drop_table(conn, "订单")
    self.assertIn("订单", [t["name"] for t in list_trashed_tables(conn)])
    self.assertNotIn("__lsm_trash_", " ".join(list_tables(conn)))

def test_restore_avoids_name_clash(conn, seeded):
    drop_table(conn, "订单")
    create_table(conn, "订单", [ColumnSpec("金额", "number")])
    restore_table(conn, "订单")
    self.assertIn("订单_2", list_tables(conn))

```

- [ ] **Step 2: 运行确认失败**。

- [ ] **Step 3: 实现** — 删表 = `ALTER TABLE ... RENAME TO "__lsm_trash_<op>_<name>"` 并把 `__lsm_table.status` 置 `trashed`；恢复反向重命名，冲突加后缀。

- [ ] **Step 4: 运行确认通过**。

- [ ] **Step 5: 提交** — `git commit -m "feat: 表重命名、删除与恢复"`。

### Task 14: 外部表启用编辑

**Files:**
- Create: `services/adopt.py`、`tests/test_adopt.py`

**Interfaces:**
- Produces: `check_adoptable(conn, table) -> tuple[bool, str]`；`adopt_table(conn, table) -> int`。

- [ ] **Step 1: 写失败测试**


```python
def test_no_pk_gets_new_id(conn):
    conn.execute('CREATE TABLE legacy ("名称" TEXT)')
    conn.execute('INSERT INTO legacy VALUES (\'a\')')
    adopt_table(conn, "legacy")
    cols = [r["name"] for r in conn.execute('PRAGMA table_info("legacy")')]
    self.assertEqual(cols[:4], ["id", "created_at", "updated_at", "deleted_at"])
    self.assertEqual(conn.execute('SELECT COUNT(*) FROM legacy').fetchone()[0], 1)

def test_text_id_rejected(conn):
    conn.execute('CREATE TABLE t ("id" TEXT PRIMARY KEY)')
    ok, reason = check_adoptable(conn, "t")
    self.assertFalse(ok)
    self.assertIn("主键", reason)

def test_composite_pk_rejected(conn):
    conn.execute('CREATE TABLE c (a TEXT, b TEXT, PRIMARY KEY (a, b))')
    self.assertFalse(check_adoptable(conn, "c")[0])

def test_unique_index_becomes_partial(conn):
    conn.execute('CREATE TABLE u (id INTEGER PRIMARY KEY, code TEXT UNIQUE)')
    adopt_table(conn, "u")
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name LIKE '%code%'").fetchone()
    self.assertIn("WHERE deleted_at IS NULL", sql["sql"])

def test_restore_conflicting_unique_raises(conn):
    conn.execute('CREATE TABLE u (id INTEGER PRIMARY KEY, code TEXT UNIQUE)')
    adopt_table(conn, "u")
    conn.execute('INSERT INTO u (code) VALUES (\'x\')')
    rid = conn.execute("SELECT id FROM u").fetchone()[0]
    soft_delete(conn, "u", [rid])
    conn.execute('INSERT INTO u (code) VALUES (\'x\')')
    with self.assertRaises(Exception):
        restore_rows(conn, "u", [rid])

```

- [ ] **Step 2: 运行确认失败**。

- [ ] **Step 3: 实现** — 12 步重建（`PRAGMA foreign_keys=OFF` → 建新表 → 拷数据 → 删旧表 → 改名 → 重建索引 → `foreign_key_check`）；已有单列整数主键则复用，不新增 `id`。

- [ ] **Step 4: 运行确认通过**。

- [ ] **Step 5: 提交** — `git commit -m "feat: 外部表启用编辑"`。

### Task 15: Excel 解析与导入

**Files:**
- Create: `services/import_excel.py`、`tests/test_import.py`、`tests/fixtures/make_fixtures.py`

**Interfaces:**
- Produces: `SheetPreview(name, columns, row_count, sample, types, suspicious, empty)`；`parse_workbook(path) -> list[SheetPreview]`；`import_sheets(conn, path, selected: list[str], types: dict[str, dict[str, str]], on_conflict: str) -> list[str]`，`on_conflict ∈ {"overwrite","copy","skip"}`。

- [ ] **Step 1: 写失败测试**（fixture 用 openpyxl 生成：一个 3 sheet 工作簿，含空 sheet、混合类型列、前导零列、重复列名）


```python
def test_preview_marks_empty_and_types(tmp_xlsx):
    previews = parse_workbook(tmp_xlsx)
    self.assertEqual([p.name for p in previews], ["订单", "空表", "客户"])
    self.assertTrue(previews[1].empty)
    self.assertEqual(previews[0].types["编码"], "text")

def test_import_creates_one_table_per_sheet(conn, tmp_xlsx):
    created = import_sheets(conn, tmp_xlsx, ["订单", "客户"], {}, "skip")
    self.assertEqual(sorted(created), ["客户", "订单"])

def test_overwrite_moves_old_table_to_trash(conn, tmp_xlsx):
    create_table(conn, "订单", [ColumnSpec("金额", "number")])
    import_sheets(conn, tmp_xlsx, ["订单"], {}, "overwrite")
    self.assertTrue(list_trashed_tables(conn))

def test_duplicate_columns_are_disambiguated(conn, tmp_xlsx_dup):
    import_sheets(conn, tmp_xlsx_dup, ["重复"], {}, "skip")
    self.assertEqual([f.name for f in get_fields(conn, "重复")][4:6], ["金额", "金额_2"])

def test_failure_rolls_back_everything(conn, tmp_xlsx_bad):
    with self.assertRaises(Exception):
        import_sheets(conn, tmp_xlsx_bad, ["订单", "客户"], {}, "skip")
    self.assertEqual(list_tables(conn), [])

```

- [ ] **Step 2: 运行确认失败**。

- [ ] **Step 3: 实现** — `openpyxl.load_workbook(read_only=True, data_only=True)`；行数用迭代计数；导入在单事务内，逐 sheet 建表并批量插入（每 500 行一次 `executemany`）；失败抛异常由调用方回滚。

- [ ] **Step 4: 运行确认通过**。

- [ ] **Step 5: 提交** — `git commit -m "feat: Excel 多 sheet 解析与导入"`。

### Task 16: 导出 Excel 与 CSV

**Files:**
- Create: `services/export_excel.py`、`tests/test_export.py`

**Interfaces:**
- Produces: `export_view(conn, table, view, out_path) -> Path`；`export_table(conn, table, out_path) -> Path`；`export_workbook(conn, tables, out_path) -> Path`；`export_csv(conn, table, columns, out_path) -> Path`。

- [ ] **Step 1: 写失败测试**


```python
def test_view_export_respects_filter_and_hidden(conn, seeded):
    view = ViewConfig(filters=[{"column": "状态", "op": "eq", "value": "完成"}], hidden=["备注"])
    path = export_view(conn, "订单", view, tmp_path / "out.xlsx")
    wb = openpyxl.load_workbook(path)
    header = [c.value for c in wb.active[1]]
    self.assertNotIn("备注", header)
    self.assertEqual(wb.active.max_row, 3)  # 表头 + 2 行

def test_multi_table_workbook(conn, two_tables):
    path = export_workbook(conn, ["订单", "客户"], tmp_path / "all.xlsx")
    self.assertEqual(openpyxl.load_workbook(path).sheetnames, ["订单", "客户"])

```

- [ ] **Step 2: 运行确认失败**。

- [ ] **Step 3: 实现** — 用 `write_only=True` 工作簿流式写；日期列写为文本（保持导入一致）；超过 100 万行抛 `ValueError`。

- [ ] **Step 4: 运行确认通过**。

- [ ] **Step 5: 提交** — `git commit -m "feat: Excel/CSV 导出"`。

### Task 17: .lsmf 分享包

**Files:**
- Create: `services/share.py`、`tests/test_share.py`

**Interfaces:**
- Produces: `export_lsmf(conn, db_path, out_path, app_version) -> Path`；`import_lsmf(path, libraries_dir) -> Path`。

- [ ] **Step 1: 写失败测试**


```python
def test_round_trip(conn, lib_path, tmp_path):
    out = export_lsmf(conn, lib_path, tmp_path / "a.lsmf", "1.0.0")
    dest = import_lsmf(out, tmp_path)
    self.assertTrue(dest.exists())
    self.assertEqual(open_library(dest).conn.execute("SELECT COUNT(*) FROM __lsm_op").fetchone()[0], 0)

def test_rejects_path_traversal(tmp_path):
    bad = tmp_path / "bad.lsmf"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("../../evil.sqlite", b"x")
        z.writestr("manifest.json", '{"format": 1}')
    with self.assertRaises(ShareError):
        import_lsmf(bad, tmp_path)

def test_rejects_newer_manifest(tmp_path):
    with self.assertRaises(ShareError):
        import_lsmf(make_lsmf(tmp_path, format=99), tmp_path)

def test_rejects_zip_bomb(tmp_path):
    with self.assertRaises(ShareError):
        import_lsmf(make_lsmf(tmp_path, fill_mb=600), tmp_path)

```

- [ ] **Step 2: 运行确认失败**。

- [ ] **Step 3: 实现** — 只接受 `manifest.json` + 单个 `db.sqlite`；解压前检查条目名与 `ZipInfo.file_size` 总和；解压到 `libraries_dir/.tmp-*` 后 `os.replace` 改名。

- [ ] **Step 4: 运行确认通过**。

- [ ] **Step 5: 提交** — `git commit -m "feat: .lsmf 导出与导入"`。

### Task 18: 表合并

**Files:**
- Create: `services/merge.py`、`tests/test_merge.py`

**Interfaces:**
- Produces: `plan_merge(conn, tables, target) -> dict`（`columns`、`conflicts`、`rows`）；`merge_tables(conn, tables, target, mapping=None) -> int`。

- [ ] **Step 1: 写失败测试**


```python
def test_aligns_columns_case_insensitively(conn, two_similar):
    plan = plan_merge(conn, ["一月", "二月"], "合计")
    self.assertIn("金额", [c.lower() for c in plan["columns"]])

def test_missing_columns_become_null(conn, two_similar):
    op = merge_tables(conn, ["一月", "二月"], "合计")
    total = conn.execute('SELECT COUNT(*) FROM "合计"').fetchone()[0]
    self.assertEqual(total, 4)
    self.assertIsNone(conn.execute('SELECT "备注" FROM "合计" WHERE "备注" IS NOT NULL LIMIT 1').fetchone())

```

- [ ] **Step 2: 运行确认失败**。

- [ ] **Step 3: 实现** — 列名归一化 `strip().lower()`；目标列类型取首个非空匹配列；写一条 `merge` op，payload 记目标表；`undo` 在 `services/undo.py` 注册表加 `merge` 处理器（目标表有后续写操作则抛 `StaleOperation`）。

- [ ] **Step 4: 运行确认通过**。

- [ ] **Step 5: 提交** — `git commit -m "feat: 多表合并"`。

### Task 19: 网格页面（模板、侧栏、分页）

**Files:**
- Create: `routes/grid.py`、`templates/base.html`、`templates/grid.html`、`templates/_sidebar.html`、`templates/_grid_rows.html`、`static/css/app.css`、`tests/test_grid_page.py`
- Modify: `app.py`（注册蓝图）

**Interfaces:**
- Consumes: `get_page`、`list_views`、`list_tables`。
- Produces: `GET /` → 302 到 `/t/<首张表>/`；`GET /t/<table>/?view=&page=` 渲染网格；模板变量 `table`、`views`、`active_view`、`page`、`fields`、`groups`。

- [ ] **Step 1: 写失败测试**


```python
def test_root_redirects_to_first_table(client_with_data):
    r = client_with_data.get("/")
    self.assertEqual(r.status_code, 302)

def test_grid_renders_rows_and_sidebar(client_with_data):
    html = client_with_data.get("/t/订单/").get_data(as_text=True)
    self.assertIn("订单", html)
    self.assertIn("全部记录", html)
    self.assertIn("SO-2026-0148", html)

def test_grid_hides_deleted_rows(client_with_data):
    html = client_with_data.get("/t/订单/").get_data(as_text=True)
    self.assertNotIn("已删除的订单号", html)

```

- [ ] **Step 2: 运行确认失败**。

- [ ] **Step 3: 实现** — 三层布局照 spec §4；`app.css` 定义色板与 34px 行高；分页链接保留当前 `view` 参数；无表时显示「导入 Excel」空态。

- [ ] **Step 4: 运行确认通过**。

- [ ] **Step 5: 提交** — `git commit -m "feat: 网格页面与侧栏"`。

### Task 20: 写入 API 与前端交互

**Files:**
- Create: `routes/grid_api.py`、`static/js/grid.js`、`static/vendor/sortable.min.js`、`tests/test_grid_api.py`
- Modify: `templates/_grid_rows.html`（加 data 属性）、`app.py`

**Interfaces:**
- Produces: `POST /api/t/<table>/save`（`{"changes": {"<id>": {...}}, "expected": {"<id>": "<updated_at>"}}` → 200 返回该行 HTML 片段；冲突 409 返回 `{"conflict": [行]}`）；`POST /api/t/<table>/insert`；`POST /api/t/<table>/delete`（`{"ids": [...]}`）；`POST /api/undo/<op_id>`。

- [ ] **Step 1: 写失败测试**


```python
def test_save_returns_row_fragment(client_with_data):
    row = client_with_data.get("/t/订单/").get_data(as_text=True)
    r = client_with_data.post("/api/t/订单/save", json={
        "changes": {"1": {"金额": "520"}}, "expected": {"1": updated_at(client_with_data, 1)}})
    self.assertEqual(r.status_code, 200)
    self.assertIn("520", r.get_data(as_text=True))

def test_stale_save_returns_409(client_with_data):
    r = client_with_data.post("/api/t/订单/save", json={
        "changes": {"1": {"金额": "1"}}, "expected": {"1": "2000-01-01T00:00:00Z"}})
    self.assertEqual(r.status_code, 409)
    self.assertIn("conflict", r.get_json())

def test_delete_then_undo(client_with_data):
    r = client_with_data.post("/api/t/订单/delete", json={"ids": [1]})
    op = r.get_json()["op"]
    client_with_data.post(f"/api/undo/{op}")
    self.assertIn("SO-2026-0148", client_with_data.get("/t/订单/").get_data(as_text=True))

```

- [ ] **Step 2: 运行确认失败**。

- [ ] **Step 3: 实现服务端** — 统一 `except ConflictError` 返回 409；返回片段用 `render_template("_grid_row.html", ...)`；所有写接口 `require_json`。

- [ ] **Step 4: 实现前端** — `grid.js`：选中/键入编辑/Enter/Tab/Esc、TSV 粘贴、批量选择、列宽与列序（SortableJS，`debounce` 400ms 存视图配置）、toast 撤销、409 时遮罩锁全部写入并重载当前页、失败可重试。

- [ ] **Step 5: 运行确认通过** — `python -m unittest tests.test_grid_api -v`；随后人工检查清单：单击选中、字符键入、双击、Enter 下移、粘贴 3×2、拖列宽、拖列序、删行后 toast 撤销、用 MCP/命令行改一行后保存触发 409 遮罩。

- [ ] **Step 6: 提交** — `git commit -m "feat: 单元格写入 API 与前端交互"`。

### Task 21: 导入向导、视图面板、历史与回收站页面

**Files:**
- Create: `routes/import_export.py`、`routes/views.py`、`routes/history.py`、`templates/import.html`、`templates/import_preview.html`、`templates/_view_panel.html`、`templates/history.html`、`templates/trash.html`、`static/js/views.js`、`tests/test_pages.py`
- Modify: `app.py`

**Interfaces:**
- Produces: `GET/POST /import`（上传 → `import_preview.html` 勾选 sheet 与类型 → 确认）；`POST /api/views`、`POST /api/views/<id>`、`DELETE /api/views/<id>`；`GET /history`、`POST /api/undo/<op>` 复用；`GET /trash`、`POST /api/trash/restore`。

- [ ] **Step 1: 写失败测试**


```python
def test_import_preview_lists_sheets(client, tmp_xlsx):
    data = {"file": (open(tmp_xlsx, "rb"), "book.xlsx")}
    html = client.post("/import", data=data, content_type="multipart/form-data").get_data(as_text=True)
    self.assertIn("订单", html)
    self.assertIn("空表", html)

def test_create_view_persists_config(client_with_data):
    r = client_with_data.post("/api/views", json={"table": "订单", "name": "本月",
        "config": {"filters": [], "sorts": [{"column": "金额", "dir": "desc"}]}})
    self.assertEqual(r.status_code, 201)

def test_trash_lists_soft_deleted(client_with_data):
    client_with_data.post("/api/t/订单/delete", json={"ids": [1]})
    self.assertIn("SO-2026-0148", client_with_data.get("/trash").get_data(as_text=True))

```

- [ ] **Step 2: 运行确认失败**。

- [ ] **Step 3: 实现** — 预览页每列一个类型下拉（含「单选」）；可疑列加 `warn` 类；空 sheet 复选框 disabled；历史页每条带「回退」按钮，失败时 flash 原因。

- [ ] **Step 4: 运行确认通过**。

- [ ] **Step 5: 提交** — `git commit -m "feat: 导入向导、视图面板与历史页面"`。

### Task 22: MCP 服务

**Files:**
- Create: `mcp_server.py`、`tests/test_mcp.py`
- Modify: `desktop/launcher.py`（Task 23 里启动）

**Interfaces:**
- Produces: `build_mcp(library, read_only=False) -> FastMCP`；工具：`list_libraries`、`list_tables`、`get_schema`、`list_views`、`get_rows`、`count_rows`、`create_rows`、`update_rows`、`delete_rows`、`restore_rows`、`create_table`、`add_field`、`merge_tables`、`get_history`、`undo`。写工具在 `read_only=True` 时抛 `PermissionError`。

- [ ] **Step 1: 写失败测试**（直接调用工具函数，不经 HTTP）


```python
def test_update_rows_writes_audit(lib):
    mcp = build_mcp(lib)
    tools = as_functions(mcp)
    tools["update_rows"](table="订单", changes=[{"id": 1, "金额": 77}])
    self.assertEqual(history(lib.conn, "订单")[0]["source"], "mcp")

def test_read_only_blocks_writes(lib):
    tools = as_functions(build_mcp(lib, read_only=True))
    with self.assertRaises(PermissionError):
        tools["delete_rows"](table="订单", ids=[1])

```

- [ ] **Step 2: 运行确认失败**。

- [ ] **Step 3: 实现** — 每个工具是薄封装：参数校验 → `services` 调用 → 返回 JSON 友好结构（`PageResult` 转 dict）；`source="mcp"`。

- [ ] **Step 4: 运行确认通过**。

- [ ] **Step 5: 提交** — `git commit -m "feat: MCP 结构化工具"`。

### Task 23: 桌面启动（单实例、托盘、日志）

**Files:**
- Create: `desktop/__init__.py`、`desktop/launcher.py`、`desktop/tray.py`、`desktop/config.py`、`desktop/logging_setup.py`、`tests/test_desktop.py`
- Modify: `pyproject.toml`（入口指向 `launcher:main`）

**Interfaces:**
- Produces: `acquire_single_instance(name) -> bool`；`pick_port(preferred) -> int`；`load_config() -> dict`；`setup_logging() -> None`；`main() -> int`。

- [ ] **Step 1: 写失败测试**


```python
def test_second_instance_refused(self):
    self.assertTrue(acquire_single_instance("lsm-test"))
    self.assertFalse(acquire_single_instance("lsm-test"))

def test_pick_port_skips_busy(self):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); s.listen()
        busy = s.getsockname()[1]
        self.assertNotEqual(pick_port(busy), busy)

```

- [ ] **Step 2: 运行确认失败**。

- [ ] **Step 3: 实现** — 单实例用 `ctypes.windll.kernel32.CreateMutexW`；`main` 依次：单实例 → 日志 → 读配置 → 开库 → 起 Flask（`make_server`，线程）→ 起 MCP（线程）→ 开浏览器 → 托盘阻塞；托盘菜单四项 + 退出。

- [ ] **Step 4: 运行确认通过**。

- [ ] **Step 5: 提交** — `git commit -m "feat: 桌面启动、单实例与托盘"`。

### Task 24: 打包脚本

**Files:**
- Create: `packaging/build.ps1`、`packaging/installer.iss`、`packaging/splash.png`、`packaging/app.ico`、`packaging/README.md`
- Modify: `.gitignore`（`build/`、`dist/`）

**Interfaces:** 无运行时代码。

- [ ] **Step 1: 写脚本** — Nuitka 参数：`--standalone --enable-plugin=no-qt --windows-console-mode=disable --include-data-dir=local_sql_manager/templates=templates --include-data-dir=local_sql_manager/static=static --windows-icon-from-ico=packaging/app.ico --windows-splash-screen=packaging/splash.png --windows-product-name=... --windows-file-version=1.0.0 --output-dir=build`；Inno Setup 打包 `build/local_sql_manager.dist`。

- [ ] **Step 2: 构建**

Run: `powershell -File packaging/build.ps1` — 期望产出 `build/local_sql_manager.dist/local-sql-manager.exe` 与 `build/installer/local-sql-manager-setup.exe`。

- [ ] **Step 3: 体积与启动验证**

Run: `(Get-ChildItem build/installer/*.exe).Length / 1MB` — 期望 ≤ 100；双击安装包 → 闪屏出现 → 浏览器打开 → 托盘出现 → 退出干净。

- [ ] **Step 4: 校验 wheel 数据文件**

Run: `python -m build --wheel && python -c "import zipfile,glob; z=zipfile.ZipFile(glob.glob('dist/*.whl')[0]); print([n for n in z.namelist() if 'templates' in n or 'static' in n][:5])"` — 期望输出里有模板与静态文件；缺失说明 pyproject 的数据文件声明未覆盖，回到 `pyproject.toml` 修 glob，再重跑本步。

- [ ] **Step 5: 提交** — `git commit -m "build: Nuitka 与 Inno Setup 打包脚本"`。

### Task 25: 清理旧代码与文档

**Files:**
- Delete: `sqlite_web/`（整目录）、`tests.py`、`setup.py`、`run-docker`、`docker/`、`MANIFEST.in`
- Modify: `README.md`、`CODEBUDDY.md`（架构段落改为新包）、`.github/workflows/tests.yaml`

**Interfaces:** 无。

- [ ] **Step 1: 删除并跑全量测试**

Run: `python -m unittest discover -s tests -v` — 期望全部 PASS，且不再 import `sqlite_web`。

- [ ] **Step 2: 更新 README** — 安装、首次启动、数据目录、MCP 配置片段、打包命令。

- [ ] **Step 3: 更新 CI** — `pip install .` 后跑 `python -m unittest discover -s tests -v`；Python 版本保持 3.10/3.12/3.14。

- [ ] **Step 4: 提交** — `git commit -m "chore: 移除旧 sqlite_web 代码，更新文档与 CI"`。
