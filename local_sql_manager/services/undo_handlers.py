"""层 6 集成件：导入的撤销处理器与统一注册入口。

导入（`import_excel.import_sheets`）成功后会写一条 `kind="import"` 的审计，
payload 形如::

    {"file": "数据.xlsx",
     "tables": ["订单", "客户"],           # 本次新建的表（按创建顺序）
     "sheets": [{"sheet": ..., "table": ..., "rows": ..., "overwritten": ...}]}

本模块把它翻译成一次可撤销的表级动作：

- 逐表调用 :func:`undo.trash_table`，把本次新建的表移入「已删除表」区；
  表已不存在（例如用户先手动删过）时跳过并记入 `missing`，不算失败。
- 任一新建表在导入之后还有未撤销的写入时拒绝撤销（复用
  :func:`undo.has_later_ops`，与删表/合并同一口径），避免把用户后来的
  修改一起吞掉。整批检查在写入前完成，失败由 undo 的保存点回滚。
- **被覆盖的旧表不会自动恢复**：覆盖导入时旧表已由 `schema.drop_table`
  记了独立审计并留在回收站，用户应从历史里撤销那条 drop_table，或手动
  恢复。否则那条 drop_table 仍显示「未撤销」而表已复活，历史与现实矛盾。
- **不支持撤销的撤销（重做）**：撤销记录本身只描述「把哪些表移进了回收站」，
  undo 的通用反向分派不认识 `undo_import` 这个动作，会抛中性的
  StaleOperation。重新导入一次即可，属于已知限制。

payload 里的 `tables` 兼容两种写法：裸表名字符串，以及带 `table` 键的
对象（历史版本与外部工具可能两种都写）。注册在模块导入时完成，
`services/__init__.py` 是唯一入口，重复导入幂等。
"""

from __future__ import annotations

import sqlite3

from . import schema, undo
from .models import StaleOperation


def _table_names(op: dict) -> list[str]:
    """从 import 审计里取出新建表名，兼容字符串与对象两种条目。"""
    payload = op.get("payload")
    raw = payload.get("tables") if isinstance(payload, dict) else None
    names: list[str] = []
    for item in raw or []:
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = item.get("table")
        else:
            name = None
        if isinstance(name, str) and name.strip():
            names.append(name)
    # 保持创建顺序去重，避免同一张表被 trash 两次。
    return list(dict.fromkeys(names))


def undo_import(
    conn: sqlite3.Connection, op: dict, source: str = "web"
) -> dict:
    """撤销一次导入：把本次新建的表移入「已删除表」区。

    返回的动作描述会并入 undo 记录的 payload。表已不存在时记入 missing；
    有后续未撤销写入时抛 StaleOperation。被覆盖的旧表保持原样留在回收站。
    """
    names = _table_names(op)
    if not names:
        raise StaleOperation("导入记录缺少表名，无法撤销")

    active = set(schema.list_tables(conn))
    missing = [name for name in names if name not in active]
    existing = [name for name in names if name in active]

    for name in existing:
        if undo.has_later_ops(conn, name, op["id"]):
            raise StaleOperation(
                f"表「{name}」在导入之后还有其它操作，请先撤销后续操作"
            )

    trashed = [
        undo.trash_table(conn, name, base_op=op["id"]) for name in existing
    ]
    return {
        "action": "undo_import",
        "tables": names,
        "trashed": trashed,
        "missing": missing,
    }


undo.register("import", undo_import)
