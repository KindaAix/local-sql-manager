"""操作级撤销：把一条审计记录反向执行，并留下可继续撤销的 undo 记录。

设计要点（见设计文档 §7.4）：

- 只有携带完整前后值的操作才能回退；`purge` 这类彻底删除标了
  `irreversible`，撤销时直接抛 :class:`Irreversible`。
- 撤销本身也是一条操作：kind 为 `undo`，payload 里的 `op` 指回被撤销
  的记录，同时用 :func:`audit.mark_undone` 标记原记录。因为 undo 自己
  也记录了完整的反向差分，所以它可以再被撤销（撤销的撤销 = 重做）。
- 目标在操作之后被改过时抛 :class:`StaleOperation`：行级按前后值比对，
  表级按名称占用与后续操作判断，绝不强行覆盖。
- 不提交事务。调用方（路由）把撤销和其它写入放进同一事务提交。

外部模块（导入、合并等）通过 :func:`register` 注册自己的 kind，处理函数
只负责执行反向操作，并返回描述本次动作的 payload；记录审计与标记原操作
都由 :func:`undo` 统一完成，因此不需要改本模块。
"""

from __future__ import annotations

import base64
import json
import sqlite3
from contextlib import contextmanager
from itertools import count
from typing import Callable

from . import audit, schema
from .models import Irreversible, StaleOperation
from .util import utcnow_iso

# 处理函数签名：(conn, op, source) -> 可选的动作 payload。
# 它必须完成实际的反向写入，但不能自己记录审计或提交事务。
Handler = Callable[[sqlite3.Connection, dict, str], "dict | None"]

_HANDLERS: dict[str, Handler] = {}

# 这些 kind 不改变数据行/表本身，不应阻塞对更早操作的撤销。注意 `undo`
# 自己也注册了处理器，但它同样不构成「后续修改」；其余豁免项一旦有处理器
# 注册就必须从这里移除（有自检测试兜底）。
_NON_DATA_KINDS = ("undo", "view", "create_field", "rename_field", "drop_field")

# 嵌套调用时保存点重名会让内层 RELEASE 直接释放外层，加序号避免复用。
_SAVEPOINT_SEQ = count(1)


def register(kind: str, handler: Handler) -> None:
    """注册（或替换）某个操作 kind 的撤销处理函数。"""
    if not isinstance(kind, str) or not kind:
        raise ValueError("操作类型必须是非空字符串")
    if not callable(handler):
        raise TypeError("撤销处理函数必须可调用")
    _HANDLERS[kind] = handler


def unregister(kind: str) -> None:
    """移除某个 kind 的撤销处理函数（测试与可选功能用）。"""
    _HANDLERS.pop(kind, None)


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


@contextmanager
def _txn(conn: sqlite3.Connection):
    """在保存点内执行撤销；不提交，失败时只回滚本次撤销。"""
    name = f"lsm_undo_{next(_SAVEPOINT_SEQ)}"
    started = not conn.in_transaction
    if started:
        conn.execute("BEGIN")
    conn.execute(f'SAVEPOINT "{name}"')
    try:
        yield
    except BaseException:
        conn.execute(f'ROLLBACK TO "{name}"')
        conn.execute(f'RELEASE "{name}"')
        if started:
            conn.execute("ROLLBACK")
        raise
    else:
        conn.execute(f'RELEASE "{name}"')


def _decode(value):
    """把审计里的 {"b64": ...} 还原成 bytes，其余原样返回。"""
    if isinstance(value, dict) and set(value) == {"b64"}:
        try:
            return base64.b64decode(value["b64"])
        except (TypeError, ValueError) as exc:
            raise StaleOperation(f"无法还原二进制值：{exc}") from exc
    return value


def _decode_row(row):
    if row is None:
        return None
    if not isinstance(row, dict):
        raise StaleOperation("操作记录的行值格式不正确")
    return {key: _decode(value) for key, value in row.items()}


def _as_id(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return value


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? COLLATE NOCASE",
        (table,),
    ).fetchone()
    if row is None:
        raise StaleOperation(f"表已不存在：{table}")
    return [item[1] for item in conn.execute(f"PRAGMA table_info({_quote(table)})")]


def _fetch_row(conn: sqlite3.Connection, table: str, row_id):
    cursor = conn.execute(
        f'SELECT * FROM {_quote(table)} WHERE "id" = ?', (row_id,)
    )
    row = cursor.fetchone()
    return dict(row) if row is not None else None


def _verify_row(row, expected, row_id, columns: list[str]) -> None:
    """目标行的当前值必须与操作时的「之后」值一致，否则视为已过期。"""
    if expected is None:
        if row is not None:
            raise StaleOperation(f"第 {row_id} 行已存在，无法重复插入")
        return
    if row is None:
        raise StaleOperation(f"第 {row_id} 行已不存在")
    for column, want in expected.items():
        if column not in columns:
            raise StaleOperation(f"列已不存在：{column}")
        if row.get(column) != want:
            raise StaleOperation(f"第 {row_id} 行的「{column}」已被修改")


def _apply_diffs(conn: sqlite3.Connection, table: str, diffs: list) -> list:
    """把每行从 diff["before"] 回退到 diff["after"]。

    before 是撤销前的状态（用于校验），after 是撤销后的目标状态；after 为
    None 表示删除该行，before 为 None 表示该行原本不存在、需要重新插入。
    返回实际执行的差分，可直接作为新 undo 记录的 rows。
    """
    columns = _table_columns(conn, table)
    if "id" not in columns:
        raise StaleOperation(f"表 {table} 没有 id 列，无法按行撤销")
    applied = []
    for item in diffs:
        if not isinstance(item, dict):
            raise StaleOperation("操作记录的行变更格式不正确")
        row_id = _as_id(item.get("id"))
        expected = _decode_row(item.get("before"))
        target = _decode_row(item.get("after"))
        row = _fetch_row(conn, table, row_id)
        _verify_row(row, expected, row_id, columns)

        if target is None:
            conn.execute(f'DELETE FROM {_quote(table)} WHERE "id" = ?', (row_id,))
        elif expected is None:
            values = {key: value for key, value in target.items() if key in columns}
            if "id" in columns and "id" not in values:
                values["id"] = row_id
            if not values:
                raise StaleOperation("操作记录缺少可写回的列")
            names = list(values)
            conn.execute(
                f'INSERT INTO {_quote(table)} '
                f'({", ".join(_quote(name) for name in names)}) '
                f'VALUES ({", ".join("?" for _ in names)})',
                list(values.values()),
            )
        else:
            values = {
                key: value
                for key, value in target.items()
                if key in columns and key != "id"
            }
            if values:
                assignments = ", ".join(
                    f"{_quote(name)} = ?" for name in values
                )
                conn.execute(
                    f'UPDATE {_quote(table)} SET {assignments} WHERE "id" = ?',
                    list(values.values()) + [row_id],
                )
        applied.append({"id": row_id, "before": expected, "after": target})
    return applied


def _invert_rows(rows: list) -> list:
    return [
        {
            "id": item.get("id"),
            "before": item.get("after"),
            "after": item.get("before"),
        }
        for item in rows
    ]


def _later_ops_touch_row(
    conn: sqlite3.Connection, table: str, op_id: int, row_ids: list
) -> set:
    """找出 op 之后仍有未撤销写入的行 id。

    行级值比对只能发现被改回同一值的写入之外的情况；这里按审计链补一层，
    与设计文档「目标之后被修改则拒绝」保持一致。已撤销的后续操作和 undo
    记录本身都不算阻塞。
    """
    wanted = {_as_id(row_id) for row_id in row_ids}
    if not wanted:
        return set()
    rows = conn.execute(
        'SELECT "payload_json" FROM "__lsm_op" WHERE "id" > ?'
        ' AND "table_name" = ? AND "undone_by" IS NULL AND "kind" <> ?',
        (op_id, table, "undo"),
    ).fetchall()
    touched = set()
    for (raw,) in rows:
        try:
            payload = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            continue
        for item in payload.get("rows") or []:
            if not isinstance(item, dict):
                continue
            row_id = _as_id(item.get("id"))
            if row_id in wanted:
                touched.add(row_id)
    return touched


def _undo_rows(conn: sqlite3.Connection, op: dict, source: str) -> dict:
    """行级撤销：把 payload.rows 的 after 状态回退到 before 状态。"""
    rows = op["payload"].get("rows")
    if not isinstance(rows, list):
        raise StaleOperation("操作缺少行变更记录")
    row_ids = [item.get("id") for item in rows if isinstance(item, dict)]
    if _later_ops_touch_row(conn, op["table"], op["id"], row_ids):
        raise StaleOperation("目标行在此之后已被修改，请先撤销后续操作")
    try:
        applied = _apply_diffs(conn, op["table"], _invert_rows(rows))
    except sqlite3.IntegrityError as exc:
        # 与「被别人改过」区分：这里应先处理唯一性冲突，而不是去撤销后续操作。
        raise StaleOperation(f"撤销与唯一约束冲突：{exc}") from exc
    return {"rows": applied}


# --- 表级动作 ---------------------------------------------------------
# 表级撤销的 payload.action 描述「这次撤销做了什么」，撤销的撤销据此反向。
# action 只有三种：drop_table（移入回收站）、restore_table（从回收站取回）、
# rename_table（改名）。


def _user_table(conn: sqlite3.Connection, table: str) -> str:
    try:
        return schema._ensure_user_table(conn, table)
    except ValueError as exc:
        raise StaleOperation(str(exc)) from exc


def _next_op_hint(conn: sqlite3.Connection) -> int:
    """即将写入的那条审计记录的大致 id，用作回收站表名的序号。"""
    row = conn.execute('SELECT COALESCE(MAX("id"), 0) + 1 FROM "__lsm_op"').fetchone()
    return int(row[0])


def _free_trash_name(conn: sqlite3.Connection, name: str, base_op: int | None) -> str:
    """生成一个空着的回收站物理名，格式与 schema 的约定一致。"""
    number = int(base_op) if base_op else _next_op_hint(conn)
    while True:
        candidate = f"{schema.TRASH_PREFIX}{number}_{name}"
        if not schema._name_taken(conn, candidate):
            return candidate
        number += 1


def _mark_trashed(conn: sqlite3.Connection, physical: str, op_id: int) -> None:
    cur = conn.execute(
        'UPDATE "__lsm_table" SET "status" = ?, "trashed_op" = ? '
        'WHERE "table_name" = ?',
        ("trashed", op_id, physical),
    )
    if cur.rowcount == 0:
        conn.execute(
            'INSERT INTO "__lsm_table"'
            ' ("table_name", "status", "created_at", "source_file", "trashed_op")'
            " VALUES (?, 'trashed', ?, NULL, ?)",
            (physical, utcnow_iso(), op_id),
        )


def trash_table(conn: sqlite3.Connection, table: str, base_op: int | None = None) -> dict:
    """把表移入「已删除表」区，返回可直接放进 undo payload 的动作描述。

    撤销导入、撤销合并等外部处理器可以调用本函数完成「把新表移入回收站」，
    再把返回值交给 :func:`register` 的处理函数。
    """
    name = _user_table(conn, table)
    hint = int(base_op) if base_op else _next_op_hint(conn)
    physical = _free_trash_name(conn, name, hint)
    schema._relocate_table(conn, name, physical)
    _mark_trashed(conn, physical, hint)
    return {"action": "drop_table", "table": name, "trashed_as": physical}


def _untrash_table(conn: sqlite3.Connection, name: str, physical: str) -> dict:
    if not schema._table_exists(conn, physical):
        raise StaleOperation(f"回收站中的表已不存在：{name}")
    if schema._name_taken(conn, name):
        raise StaleOperation(f"表名已被占用：{name}")
    schema._relocate_table(conn, physical, name)
    conn.execute(
        'UPDATE "__lsm_table" SET "status" = ?, "trashed_op" = NULL '
        'WHERE "table_name" = ?',
        ("active", name),
    )
    return {"action": "restore_table", "table": name, "trashed_as": physical}


def _rename_action(conn: sqlite3.Connection, old: str, new: str) -> dict:
    if not schema._table_exists(conn, old):
        raise StaleOperation(f"表已不存在：{old}")
    if schema._name_taken(conn, new):
        raise StaleOperation(f"表名已被占用：{new}")
    schema._relocate_table(conn, old, new)
    return {"action": "rename_table", "old": old, "new": new}


def _has_later_ops(conn: sqlite3.Connection, table: str, op_id: int) -> bool:
    """该表在 op 之后是否还有未撤销的数据写入。

    undo 记录不算：撤销一条后续写入正是解除阻塞的方式；视图/字段元数据
    操作也没有撤销处理器，若算阻塞会让撤销建表变成死路。
    """
    placeholders = ", ".join("?" for _ in _NON_DATA_KINDS)
    row = conn.execute(
        'SELECT 1 FROM "__lsm_op" WHERE "id" > ? AND "table_name" = ?'
        f' AND "undone_by" IS NULL AND "kind" NOT IN ({placeholders}) LIMIT 1',
        (op_id, table, *_NON_DATA_KINDS),
    ).fetchone()
    return row is not None


def _op_table_name(op: dict) -> str:
    payload = op["payload"]
    return payload.get("name") or payload.get("table") or op["table"]


def _undo_create_table(conn: sqlite3.Connection, op: dict, source: str) -> dict:
    name = _user_table(conn, _op_table_name(op))
    if _has_later_ops(conn, name, op["id"]):
        raise StaleOperation("该表在此之后还有其它操作，请先撤销后续操作")
    return trash_table(conn, name, base_op=op["id"])


def _undo_rename_table(conn: sqlite3.Connection, op: dict, source: str) -> dict:
    payload = op["payload"]
    old = payload.get("old")
    new = payload.get("new")
    if not old or not new:
        raise StaleOperation("操作记录缺少表名")
    return _rename_action(conn, new, old)


def _undo_drop_table(conn: sqlite3.Connection, op: dict, source: str) -> dict:
    payload = op["payload"]
    name = _op_table_name(op)
    physical = payload.get("trashed_as") or f"{schema.TRASH_PREFIX}{op['id']}_{name}"
    return _untrash_table(conn, name, physical)


def _undo_restore_table(conn: sqlite3.Connection, op: dict, source: str) -> dict:
    payload = op["payload"]
    restored_as = payload.get("restored_as") or _op_table_name(op)
    return trash_table(conn, restored_as, base_op=op["id"])


def _invert_action(conn: sqlite3.Connection, action: dict, base_op: int | None) -> dict:
    kind = action.get("action") if isinstance(action, dict) else None
    if kind == "drop_table":
        return _untrash_table(conn, action["table"], action["trashed_as"])
    if kind == "restore_table":
        return trash_table(conn, action["table"], base_op=base_op)
    if kind == "rename_table":
        return _rename_action(conn, action["new"], action["old"])
    raise StaleOperation(f"无法撤销的表级动作：{kind}")


def _undo_irreversible(conn: sqlite3.Connection, op: dict, source: str) -> dict:
    """彻底删除没有可回退的内容；即使审计标志缺失也按不可撤销处理。"""
    raise Irreversible("彻底删除不可撤销")


def _undo_action(conn: sqlite3.Connection, op: dict, source: str) -> dict:
    """撤销一条 undo 记录：反向执行它当时做过的动作。"""
    payload = op["payload"]
    if "rows" in payload:
        return _undo_rows(conn, op, source)
    if "actions" in payload:
        performed = [
            _invert_action(conn, action, op["id"])
            for action in reversed(payload["actions"])
        ]
        return {"actions": performed}
    if "action" in payload:
        return _invert_action(conn, payload, op["id"])
    raise StaleOperation("无法识别的撤销记录")


def undo(conn: sqlite3.Connection, op_id: int, source: str = "web") -> int:
    """撤销一条操作，返回新建 undo 记录的 id。

    不可撤销、已被撤销或目标已变化时抛出异常，且不留下任何写入。
    """
    # 路由可能把表单/URL 里的字符串 id 直接传进来。
    op_id = _as_id(op_id)
    with _txn(conn):
        # 在事务内读取，确保校验与写入看到的是同一份操作记录。
        op = audit.get_op(conn, op_id)
        if op is None:
            raise ValueError(f"操作不存在：{op_id}")
        if op["irreversible"]:
            raise Irreversible("该操作不可撤销")
        if op["undone_by"] is not None:
            raise StaleOperation("该操作已被撤销")
        handler = _HANDLERS.get(op["kind"])
        if handler is None:
            raise StaleOperation(f"不支持撤销的操作类型：{op['kind']}")

        extra = handler(conn, op, source) or {}
        payload = {**extra, "op": op_id}
        undo_id = audit.record(conn, "undo", op["table"], source, payload)
        if audit.mark_undone(conn, op_id, undo_id) != 1:
            raise StaleOperation("该操作已被撤销")
    return undo_id


register("insert", _undo_rows)
register("update", _undo_rows)
register("delete", _undo_rows)
register("restore", _undo_rows)
register("undo", _undo_action)
register("create_table", _undo_create_table)
register("rename_table", _undo_rename_table)
register("drop_table", _undo_drop_table)
register("restore_table", _undo_restore_table)
register("purge", _undo_irreversible)
register("purge_table", _undo_irreversible)
