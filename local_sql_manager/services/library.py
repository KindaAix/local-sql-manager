"""库文件的打开与元数据初始化。"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from .models import UnsupportedFormat

logger = logging.getLogger(__name__)

META_VERSION = 1

# 元数据表结构，见设计文档 §6。界面不展示这些表。
SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS "__lsm_meta" (
        "key" TEXT PRIMARY KEY,
        "value" TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "__lsm_table" (
        "table_name" TEXT PRIMARY KEY,
        "status" TEXT,
        "created_at" TEXT,
        "source_file" TEXT,
        "trashed_op" INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "__lsm_field" (
        "table_name" TEXT,
        "column" TEXT,
        "type" TEXT,
        "options_json" TEXT,
        "position" INTEGER,
        "width" INTEGER,
        PRIMARY KEY ("table_name", "column")
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "__lsm_view" (
        "id" INTEGER PRIMARY KEY,
        "table_name" TEXT,
        "name" TEXT,
        "config_json" TEXT,
        "position" INTEGER,
        "is_default" INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "__lsm_op" (
        "id" INTEGER PRIMARY KEY,
        "ts" TEXT,
        "source" TEXT,
        "kind" TEXT,
        "table_name" TEXT,
        "payload_json" TEXT,
        "undone_by" INTEGER,
        "irreversible" INTEGER DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "__lsm_trash_col" (
        "op_id" INTEGER,
        "table_name" TEXT,
        "column" TEXT,
        "type" TEXT,
        "row_id" INTEGER,
        "value" TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS "__lsm_op_ts"
    ON "__lsm_op" ("ts", "id")
    """,
    """
    CREATE INDEX IF NOT EXISTS "__lsm_op_table_ts"
    ON "__lsm_op" ("table_name", "ts", "id")
    """,
)


@dataclass
class Library:
    """一个已打开的库。"""

    conn: sqlite3.Connection
    path: str


def _has_meta(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='__lsm_meta'"
    ).fetchone()
    return row is not None


def ensure_meta(conn: sqlite3.Connection) -> None:
    """建立元数据表，并把格式版本校正为当前版本。"""
    for statement in SCHEMA:
        conn.execute(statement)
    conn.execute(
        """
        INSERT INTO "__lsm_meta" ("key", "value") VALUES (?, ?)
        ON CONFLICT("key") DO UPDATE SET "value" = excluded."value"
        """,
        ("version", str(META_VERSION)),
    )
    conn.commit()


def meta_version(conn: sqlite3.Connection) -> int:
    """读取库格式版本；没有版本记录时返回 0。"""
    row = conn.execute(
        'SELECT "value" FROM "__lsm_meta" WHERE "key" = ?', ("version",)
    ).fetchone()
    if row is None:
        return 0
    raw = row[0]
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise UnsupportedFormat(f"库格式版本无法识别：{raw!r}") from None


def _connect(path: str) -> sqlite3.Connection:
    """打开一条库连接；独立成函数便于测试替换。"""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def open_library(path: str) -> Library:
    """打开库文件，校验格式版本后准备好元数据表。"""
    conn = _connect(path)
    try:
        # WAL：读写不互斥，读事务不会挡住写事务。rollback journal 下服务层
        # 「先读后写」需要把 SHARED 升级成 RESERVED，这一步不走 busy handler，
        # 并发写会立刻 database is locked。/ share.py 用 backup 接口导出，WAL 安全。
        #
        # 但 WAL 依赖同目录的 -shm，网络盘、OneDrive 占位目录或只读介质上
        # 可能建不出来；那种情况下开库不该直接失败，退回默认 journal 模式，
        # 只是并发写会退化为等待/失败，功能本身还能用。
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error as exc:
            logger.warning("无法启用 WAL，改用默认 journal 模式：%s", exc)
        conn.execute("PRAGMA foreign_keys=ON")
        if _has_meta(conn):
            version = meta_version(conn)
            if version > META_VERSION:
                raise UnsupportedFormat(
                    f"库格式版本 {version} 高于当前应用支持的 {META_VERSION}"
                )
        ensure_meta(conn)
    except Exception:
        conn.close()
        raise
    return Library(conn=conn, path=path)
