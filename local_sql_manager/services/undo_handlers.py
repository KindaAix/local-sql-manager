"""层 6 集成件：导入的撤销处理器与统一注册入口。

导入（`import_excel.import_sheets`）成功后会写一条 `kind="import"` 的审计，
payload 形如::

    {"file": "数据.xlsx",
     "tables": ["订单", "客户"],           # 本次新建的表（按创建顺序）
     "sheets": [{"sheet": ..., "table": ..., "rows": ..., "overwritten": ...}]}

本模块把它翻译成一次表级撤销：

- 表名只从 `payload["tables"]` 取：允许表名字符串列表、带 `table` 键的对象
  列表，也允许整字段就是一个字符串；每个名字 strip 后按 casefold 与当前活动
  表匹配，避免大小写或多余空格导致「找到了却搬不动」。
- 匹配不到活动表时先做 fail-closed 检查：该表名在 import op 之后若还有未撤销
  的写操作（`undo` / `drop_table` / `purge_table` 之外），说明表被改过名或
  换了身份，直接抛 StaleOperation，而不是静默跳过；只有确实已被删进回收站或
  彻底删除时才记入 `missing` 跳过。
- 仍在活动的表沿用 :func:`undo.has_later_ops` 判断后续写入，任一表被改动就
  整批拒绝；所有检查都在写表之前完成，失败由 undo 的保存点回滚。
- **被覆盖的旧表不会自动恢复**：覆盖导入时旧表已由 `schema.drop_table`
  记了独立审计并留在回收站，用户应从历史里撤销那条 drop_table，或手动
  恢复。否则那条 drop_table 仍显示「未撤销」而表已复活，历史与现实矛盾。
- 返回 `{"actions": [...], "tables": [...], "missing": [...]}`，其中 actions 是
  逐表移入回收站的动作描述。undo 的通用反向分派会据此把表恢复回来，因此
  「撤销的撤销」等于重做这次导入。

注册在模块导入时完成；`services/__init__.py` 负责导入本模块，重复导入幂等。
"""

from __future__ import annotations

import sqlite3

from . import schema, undo
from .models import StaleOperation

# 表已不在活动集合时，这些 kind 不算「表被改动过」：
#   undo        —— 撤销本身不是新的写入；
#   drop_table  —— 用户把表移进回收站；
#   purge_table —— 用户彻底删除。
# 其余 kind（改名、建同名列、写行、加字段……）都说明表的身份变了，必须拒绝。
_MISSING_SAFE_KINDS = ("undo", "drop_table", "purge_table")


def _table_names(op: dict) -> list[str]:
    """从 import 审计里取出新建表名，容忍字符串列表/对象列表/单个字符串。"""
    payload = op.get("payload")
    raw = payload.get("tables") if isinstance(payload, dict) else None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise StaleOperation("导入记录的表名格式不正确")

    names: list[str] = []
    for item in raw:
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = item.get("table")
        else:
            name = None
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    if not names:
        raise StaleOperation("导入记录缺少表名，无法撤销")
    # 保持创建顺序去重，避免同一张表被 trash 两次。
    return list(dict.fromkeys(names))


def _touched_after_import(conn: sqlite3.Connection, name: str, op_id: int) -> bool:
    """表名在 import 之后是否有「改身份」的未撤销操作（大小写不敏感）。"""
    placeholders = ", ".join("?" for _ in _MISSING_SAFE_KINDS)
    row = conn.execute(
        'SELECT 1 FROM "__lsm_op" WHERE "id" > ?'
        ' AND "table_name" COLLATE NOCASE = ?'
        ' AND "undone_by" IS NULL'
        f' AND "kind" NOT IN ({placeholders}) LIMIT 1',
        (op_id, name, *_MISSING_SAFE_KINDS),
    ).fetchone()
    return row is not None


def undo_import(
    conn: sqlite3.Connection, op: dict, source: str = "web"
) -> dict:
    """撤销一次导入：把本次新建的表移入「已删除表」区。

    返回的动作描述会并入 undo 记录的 payload；撤销这条 undo 时按 actions
    反向执行，把表恢复回来。表已不存在时记入 missing；有后续未撤销写入时
    抛 StaleOperation。被覆盖的旧表保持原样留在回收站。
    """
    names = _table_names(op)
    active = {table.casefold(): table for table in schema.list_tables(conn)}

    missing: list[str] = []
    existing: list[tuple[str, str]] = []
    for name in names:
        actual = active.get(name.casefold())
        if actual is None:
            # 找不到活动表时不能直接当 missing：改名过的表会让撤销静默空跑，
            # 却把 import 标成已撤销，之后再也撤不掉。
            if _touched_after_import(conn, name, op["id"]):
                raise StaleOperation(
                    f"表「{name}」在导入之后被改动过，请先撤销后续操作"
                )
            missing.append(name)
            continue
        if undo.has_later_ops(conn, actual, op["id"]):
            raise StaleOperation(
                f"表「{actual}」在导入之后还有其它操作，请先撤销后续操作"
            )
        existing.append((name, actual))

    trashed = [
        undo.trash_table(conn, actual, base_op=op["id"])
        for _name, actual in existing
    ]
    return {"actions": trashed, "tables": names, "missing": missing}


undo.register("import", undo_import)
