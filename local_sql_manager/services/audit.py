"""操作审计：写入与查询 __lsm_op。

所有写路径都必须先经由本模块记录一条操作（见设计文档 §7.4）。
record / mark_undone 不开事务、不提交，调用方把它们放进同一事务里，
保证「改数据 + 记审计」原子生效。
"""

from __future__ import annotations

import base64
import json
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from .util import utcnow_iso


def _json_default(value: Any) -> Any:
    """把 JSON 不认识的类型转成可存的形式。

    bytes 与 executor 的行标识约定保持一致，编成 {"b64": ...}；不要用
    default=str，否则 BLOB 会被静默写成一串 b'...'。
    """
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"b64": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"无法序列化为 JSON 的类型：{type(value).__name__}")


_SELECT = (
    'SELECT "id", "ts", "source", "kind", "table_name", "payload_json",'
    ' "undone_by", "irreversible" FROM "__lsm_op"'
)


def _to_dict(row: tuple) -> dict:
    return {
        "id": row[0],
        "ts": row[1],
        "source": row[2],
        "kind": row[3],
        "table": row[4],
        "payload": json.loads(row[5]) if row[5] is not None else {},
        "undone_by": row[6],
        "irreversible": row[7],
    }


def record(
    conn: sqlite3.Connection,
    kind: str,
    table: str,
    source: str,
    payload: dict | None = None,
    irreversible: bool = False,
) -> int:
    """写入一条操作记录，返回 __lsm_op.id；payload 为 None 时记为 {}。

    不提交事务，由调用方与数据写入放进同一事务。
    """
    if payload is None:
        payload = {}
    cur = conn.execute(
        """
        INSERT INTO "__lsm_op"
            ("ts", "source", "kind", "table_name", "payload_json", "irreversible")
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            utcnow_iso(),
            source,
            kind,
            table,
            json.dumps(payload, ensure_ascii=False, default=_json_default),
            1 if irreversible else 0,
        ),
    )
    return cur.lastrowid


def get_op(conn: sqlite3.Connection, op_id: int) -> dict | None:
    """按 id 取一条操作；不存在时返回 None。"""
    row = conn.execute(f'{_SELECT} WHERE "id" = ?', (op_id,)).fetchone()
    if row is None:
        return None
    return _to_dict(row)


def history(conn: sqlite3.Connection, table: str | None = None, limit: int = 50) -> list[dict]:
    """按时间倒序返回操作历史，可按表名筛选。

    limit 收敛到 1..500：大于 500 按 500 截断，小于等于 0 返回空列表。
    不提交事务。
    """
    if limit <= 0:
        return []
    limit = min(limit, 500)
    sql = _SELECT
    params: list[Any] = []
    if table is not None:
        sql += ' WHERE "table_name" = ?'
        params.append(table)
    sql += ' ORDER BY "ts" DESC, "id" DESC LIMIT ?'
    params.append(limit)
    return [_to_dict(row) for row in conn.execute(sql, params)]


def mark_undone(conn: sqlite3.Connection, op_id: int, undo_id: int) -> int:
    """把原操作标记为已被某条撤销操作回退，返回受影响行数。

    只标记尚未撤销的记录：op 不存在或已被撤销时返回 0。不提交事务。
    """
    cur = conn.execute(
        'UPDATE "__lsm_op" SET "undone_by" = ?'
        ' WHERE "id" = ? AND "undone_by" IS NULL',
        (undo_id, op_id),
    )
    return cur.rowcount