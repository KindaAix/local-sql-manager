"""操作审计：写入与查询 __lsm_op。

所有写路径都必须先经由本模块记录一条操作（见设计文档 §7.4）。
record / mark_undone 不开事务、不提交，调用方把它们放进同一事务里，
保证「改数据 + 记审计」原子生效。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .util import utcnow_iso

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
    payload: dict,
    irreversible: bool = False,
) -> int:
    """写入一条操作记录，返回 __lsm_op.id。"""
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
            json.dumps(payload, ensure_ascii=False),
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
    """按时间倒序返回操作历史，可按表名筛选。"""
    sql = _SELECT
    params: list[Any] = []
    if table is not None:
        sql += ' WHERE "table_name" = ?'
        params.append(table)
    sql += ' ORDER BY "ts" DESC, "id" DESC LIMIT ?'
    params.append(limit)
    return [_to_dict(row) for row in conn.execute(sql, params)]


def mark_undone(conn: sqlite3.Connection, op_id: int, undo_id: int) -> None:
    """把原操作标记为已被某条撤销操作回退。"""
    conn.execute(
        'UPDATE "__lsm_op" SET "undone_by" = ? WHERE "id" = ?', (undo_id, op_id)
    )
