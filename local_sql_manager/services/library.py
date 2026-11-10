"""库文件的打开与元数据初始化。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .models import UnsupportedFormat

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
)


@dataclass
class Library:
    """一个已打开的库。"""

    conn: sqlite3.Connection
    path: str


def ensure_meta(conn: sqlite3.Connection) -> None:
    """建立元数据表，并在缺失时写入当前格式版本。"""
    for statement in SCHEMA:
        conn.execute(statement)
    conn.execute(
        'INSERT OR IGNORE INTO "__lsm_meta" ("key", "value") VALUES (?, ?)',
        ("version", str(META_VERSION)),
    )
    conn.commit()


def meta_version(conn: sqlite3.Connection) -> int:
    """读取库格式版本；没有版本记录时返回 0。"""
    row = conn.execute(
        'SELECT "value" FROM "__lsm_meta" WHERE "key" = ?', ("version",)
    ).fetchone()
    return int(row[0]) if row else 0


def open_library(path: str) -> Library:
    """打开库文件，准备好元数据表并校验格式版本。"""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_meta(conn)

    version = meta_version(conn)
    if version > META_VERSION:
        conn.close()
        raise UnsupportedFormat(
            f"库格式版本 {version} 高于当前应用支持的 {META_VERSION}"
        )
    return Library(conn=conn, path=path)
