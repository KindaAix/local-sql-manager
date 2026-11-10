"""字段级管理：增列、改名、删列、改类型，以及它们在撤销里的反向动作。

设计（见设计文档 §7.10 与 §7.4）：

- 增列走 ALTER TABLE ADD COLUMN（可空），既有行的新列为 NULL；列顺序由
  __lsm_field.position 决定，插在中间时后面的列顺延。
- 改名走 ALTER TABLE RENAME COLUMN；SQLite 不会改索引名，所以先删旧索引名
  再补建，并把该表所有视图配置里的列引用一起改写（纯配置跟随，不记 view
  审计）。
- 删列前先把非空值按 (row_id, value) 备份进 __lsm_trash_col；value 用 JSON
  文本存储，bytes 编成 {"b64": ...}，与审计 payload 同一约定。备份完之后才
  删索引、删列、删元数据、清理视图引用。
- 改类型只动 __lsm_field，不重建物理列。读取/筛选/排序/汇总按逻辑类型
  CAST 是层 2（query/rows）的职责，目前尚未实现，所以改类型只影响元数据与
  之后的界面展示。
- 四个操作都只写审计、不 commit。删列的撤销处理器返回描述动作的简要
  payload；字段动作暂不支持「撤销的撤销」（层 3 的 undo 只认识表级动作，
  要重做需要 undo 开放 action 分派扩展点，等层 7 统一处理）。

对 schema 内部设施的依赖（同属服务层，包内直接调用；schema 若改这些私有
名字，本模块需要同步）：

- schema._ensure_user_table：校验受管表并取回库里的真实表名；
- schema._quote / schema._index_name：标识符引用与自动索引命名；
- schema.ensure_indexes：增列/改名后补建索引；
- schema.get_fields / schema.ColumnSpec / schema.PHYSICAL_TYPES。

撤销处理器在模块导入时注册，重复导入幂等（undo.register 是覆盖语义）。
"""

from __future__ import annotations

import base64
import json
import sqlite3
from contextlib import contextmanager
from itertools import count

from . import audit, schema, undo
from .models import Field, StaleOperation
from .types import TYPES

# 受管表自带的列，业务上不允许重名（大小写不敏感）。
RESERVED_COLUMNS = ("id", "created_at", "updated_at", "deleted_at", "rowid")

# 允许带选项列表的字段类型。
_OPTION_TYPES = ("select", "multiselect")

_SOURCE = "web"

# 视图配置里带 column 键的条目列表，以及纯列名列表。
_COLUMN_ITEMS = ("filters", "sorts", "aggregates")
# 顺序有语义的条目列表：撤销时要插回原来的位置，不能简单追加。
_ORDERED_ITEMS = ("sorts", "aggregates")
_COLUMN_NAMES = ("hidden", "order")

# 保存点重名时内层 RELEASE 会释放外层，加序号避免复用。
_SAVEPOINT_SEQ = count(1)


@contextmanager
def _txn(conn: sqlite3.Connection):
    """在保存点内执行，失败只回滚本次动作；始终不提交。

    schema._savepoint 在调用方没有开启事务时会随 RELEASE 自动提交，本模块
    要求「一律不 commit」，所以这里自己 BEGIN + SAVEPOINT，把事务留给调用方。
    """
    name = f"lsm_field_{next(_SAVEPOINT_SEQ)}"
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


# --- 公共小工具 -------------------------------------------------------


def _quote(name: str) -> str:
    return schema._quote(name)


def _same(left, right) -> bool:
    """列名比较按 NOCASE，与 SQLite 的标识符语义一致。"""
    if left is None or right is None:
        return False
    return str(left).casefold() == str(right).casefold()


def _fields(conn: sqlite3.Connection, table: str) -> list[Field]:
    return schema.get_fields(conn, table)


def _find(fields_list: list[Field], name) -> Field | None:
    for field in fields_list:
        if _same(field.name, name):
            return field
    return None


def _physical_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({_quote(table)})")]


def _ensure_managed(conn: sqlite3.Connection, table: str) -> str:
    """校验表是受管表：真实存在的用户表，且已在 __lsm_table 登记为可用。

    外部表要先走「启用编辑」登记元数据，否则字段操作会在没有 __lsm_table
    行的情况下留下孤儿 __lsm_field 记录。
    """
    table = schema._ensure_user_table(conn, table)
    row = conn.execute(
        'SELECT "status" FROM "__lsm_table" WHERE "table_name" = ?', (table,)
    ).fetchone()
    if row is None or row[0] != "active":
        raise ValueError(f"表未启用编辑：{table}")
    return table


def _check_type(type_: str) -> str:
    if type_ not in TYPES:
        raise ValueError(f"未知字段类型：{type_}")
    return type_


def _check_options(type_: str, options) -> tuple:
    """校验选项列表：必须是列表/元组、元素非空文本，且只有单选/多选可带。

    裸字符串会被 tuple() 拆成单个字符，所以这里先卡类型，不做隐式转换。
    """
    if options is None:
        cleaned = ()
    elif isinstance(options, (list, tuple)):
        cleaned = tuple(options)
    else:
        raise ValueError("字段选项必须是列表")
    for item in cleaned:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("字段选项必须是非空文本")
    if cleaned and type_ not in _OPTION_TYPES:
        raise ValueError(f"{type_} 类型不支持选项")
    return cleaned


def _options_json(options: tuple) -> str | None:
    return json.dumps(list(options), ensure_ascii=False) if options else None


def _validate_name(
    conn: sqlite3.Connection, table: str, name: str, *, ignore: str | None = None
) -> str:
    """校验一个新列名：非空文本、非保留名、不与现有列/物理列撞名。"""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("列名必须是非空文本")
    name = name.strip()
    if name.casefold() in {item.casefold() for item in RESERVED_COLUMNS}:
        raise ValueError(f"列名不能使用保留名：{name}")
    for other in _fields(conn, table):
        if _same(other.name, name) and not _same(other.name, ignore):
            raise ValueError(f"列已存在：{name}")
    for physical in _physical_columns(conn, table):
        if _same(physical, name) and not _same(physical, ignore):
            raise ValueError(f"列已存在：{name}")
    return name


def _insert_position(
    conn: sqlite3.Connection, table: str, after: str | None, fields_list: list[Field]
) -> int:
    """算新列的 position：插在 after 之后并把后面的列顺延，None 追加到末尾。"""
    if after is None:
        return max((field.position for field in fields_list), default=-1) + 1
    anchor = _find(fields_list, after)
    if anchor is None:
        raise ValueError(f"列不存在：{after}")
    position = anchor.position + 1
    conn.execute(
        'UPDATE "__lsm_field" SET "position" = "position" + 1'
        ' WHERE "table_name" = ? AND "position" >= ?',
        (table, position),
    )
    return position


# --- 视图配置跟随 -----------------------------------------------------


def _load_json_object(raw: str | None) -> dict | None:
    """把 JSON 文本还原成 dict；坏数据显示为 None，避免整块配置被覆盖。"""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _view_entries(conn: sqlite3.Connection, table: str):
    rows = conn.execute(
        'SELECT "id", "config_json" FROM "__lsm_view" WHERE "table_name" = ?'
        ' ORDER BY "id"',
        (table,),
    ).fetchall()
    for row in rows:
        config = _load_json_object(row[1])
        if config is not None:
            yield row[0], config


def _save_config(conn: sqlite3.Connection, view_id: int, config: dict) -> None:
    conn.execute(
        'UPDATE "__lsm_view" SET "config_json" = ? WHERE "id" = ?',
        (json.dumps(config, ensure_ascii=False), view_id),
    )


def _rename_view_refs(
    conn: sqlite3.Connection, table: str, old: str, new: str
) -> None:
    """把该表所有视图配置里的 old 列引用改成 new（保留未知键）。"""
    for view_id, config in _view_entries(conn, table):
        changed = False
        for key in _COLUMN_ITEMS:
            items = config.get(key)
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and _same(item.get("column"), old):
                    item["column"] = new
                    changed = True
        for key in _COLUMN_NAMES:
            items = config.get(key)
            if not isinstance(items, list):
                continue
            for index, item in enumerate(items):
                if isinstance(item, str) and _same(item, old):
                    items[index] = new
                    changed = True
        widths = config.get("widths")
        if isinstance(widths, dict):
            for key in list(widths):
                if _same(key, old):
                    widths[new] = widths.pop(key)
                    changed = True
        group = config.get("group")
        if isinstance(group, dict) and _same(group.get("column"), old):
            group["column"] = new
            changed = True
        if changed:
            _save_config(conn, view_id, config)


def _pop_view_refs(config: dict, column: str) -> dict | None:
    """从一份视图配置里摘掉 column 的所有引用，返回被摘掉的条目。"""
    removed = {
        "filters": [],
        "sorts": [],
        "aggregates": [],
        "hidden": [],
        "order": [],
        "widths": [],
        "group": None,
    }
    changed = False
    for key in _COLUMN_ITEMS:
        items = config.get(key)
        if not isinstance(items, list):
            continue
        kept = []
        for index, item in enumerate(items):
            if isinstance(item, dict) and _same(item.get("column"), column):
                # sorts/aggregates 的顺序有语义，记下原位置供撤销插回。
                if key in _ORDERED_ITEMS:
                    removed[key].append({"index": index, "item": item})
                else:
                    removed[key].append(item)
            else:
                kept.append(item)
        if len(kept) != len(items):
            config[key] = kept
            changed = True
    hidden = config.get("hidden")
    if isinstance(hidden, list):
        removed["hidden"] = [
            item for item in hidden if isinstance(item, str) and _same(item, column)
        ]
        kept = [item for item in hidden if not _same(item, column)]
        if len(kept) != len(hidden):
            config["hidden"] = kept
            changed = True
    order = config.get("order")
    if isinstance(order, list):
        removed["order"] = [
            {"index": index, "column": item}
            for index, item in enumerate(order)
            if isinstance(item, str) and _same(item, column)
        ]
        kept = [item for item in order if not _same(item, column)]
        if len(kept) != len(order):
            config["order"] = kept
            changed = True
    widths = config.get("widths")
    if isinstance(widths, dict):
        for key in list(widths):
            if _same(key, column):
                removed["widths"].append({"column": key, "width": widths[key]})
                del widths[key]
                changed = True
    group = config.get("group")
    if isinstance(group, dict) and _same(group.get("column"), column):
        removed["group"] = group
        config["group"] = None
        changed = True
    return removed if changed else None


def _restore_view_refs(config: dict, column: str, refs: dict) -> bool:
    """把 _pop_view_refs 摘掉的条目放回去；列已存在时跳过，避免重复。"""
    changed = False
    for key in _COLUMN_ITEMS:
        items = refs.get(key) or []
        if not items:
            continue
        current = config.get(key)
        if not isinstance(current, list):
            current = []
        if any(
            isinstance(item, dict) and _same(item.get("column"), column)
            for item in current
        ):
            continue
        if key in _ORDERED_ITEMS:
            # 插回原来的位置，否则删中间一项再撤销会静默改变排序优先级。
            for entry in sorted(items, key=lambda item: item.get("index") or 0):
                index = min(int(entry.get("index") or 0), len(current))
                current.insert(index, entry.get("item"))
        else:
            current = current + list(items)
        config[key] = current
        changed = True
    hidden = refs.get("hidden") or []
    if hidden:
        current = config.get("hidden")
        if not isinstance(current, list):
            current = []
        if not any(_same(item, column) for item in current):
            config["hidden"] = current + list(hidden)
            changed = True
    order = refs.get("order") or []
    if order:
        current = config.get("order")
        if not isinstance(current, list):
            current = []
        if not any(_same(item, column) for item in current):
            for entry in sorted(order, key=lambda item: item.get("index") or 0):
                index = min(int(entry.get("index") or 0), len(current))
                current.insert(index, entry.get("column"))
            config["order"] = current
            changed = True
    widths = refs.get("widths") or []
    if widths:
        current = config.get("widths")
        if not isinstance(current, dict):
            current = {}
        touched = False
        for entry in widths:
            if any(_same(key, column) for key in current):
                continue
            current[entry.get("column")] = entry.get("width")
            touched = True
        if touched:
            config["widths"] = current
            changed = True
    group = refs.get("group")
    if isinstance(group, dict) and config.get("group") is None:
        config["group"] = group
        changed = True
    return changed


def _collect_view_refs(
    conn: sqlite3.Connection, table: str, column: str
) -> list[dict]:
    """从所有视图配置里摘掉 column 引用，返回每个视图被摘掉的条目。"""
    collected = []
    for view_id, config in _view_entries(conn, table):
        removed = _pop_view_refs(config, column)
        if removed is not None:
            _save_config(conn, view_id, config)
            collected.append({"view_id": view_id, **removed})
    return collected


def _put_view_refs(
    conn: sqlite3.Connection, table: str, column: str, refs: list
) -> None:
    by_view = {
        entry["view_id"]: entry
        for entry in refs
        if isinstance(entry, dict) and isinstance(entry.get("view_id"), int)
    }
    if not by_view:
        return
    for view_id, config in _view_entries(conn, table):
        entry = by_view.get(view_id)
        if entry is None:
            continue
        if _restore_view_refs(config, column, entry):
            _save_config(conn, view_id, config)


# --- 列值编解码（与审计 payload 的 bytes 约定一致）--------------------


def _encode_value(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"b64": base64.b64encode(bytes(value)).decode("ascii")}
    return value


def _decode_value(value):
    if isinstance(value, dict) and set(value) == {"b64"}:
        return base64.b64decode(value["b64"])
    return value


# --- 物理列动作（公开函数与撤销处理器共用）----------------------------


def _rename_column(
    conn: sqlite3.Connection, table: str, old: str, new: str
) -> None:
    """物理改名 + 元数据 + 索引名 + 视图引用，四者保持一致。"""
    conn.execute(
        f"ALTER TABLE {_quote(table)} RENAME COLUMN {_quote(old)} TO {_quote(new)}"
    )
    conn.execute(
        'UPDATE "__lsm_field" SET "column" = ?'
        ' WHERE "table_name" = ? AND "column" = ?',
        (new, table, old),
    )
    # RENAME COLUMN 保留旧索引名，必须显式删掉，否则新名字的索引建不出来。
    conn.execute(f"DROP INDEX IF EXISTS {_quote(schema._index_name(table, old))}")
    schema.ensure_indexes(conn, table)
    _rename_view_refs(conn, table, old, new)


def _indexes_for_column(
    conn: sqlite3.Connection, table: str, column: str
) -> list[str]:
    """表上所有引用了该列的索引名（含外部表启用编辑时带来的旧索引）。"""
    found = []
    for row in conn.execute(f"PRAGMA index_list({_quote(table)})").fetchall():
        index = row[1]
        for info in conn.execute(f"PRAGMA index_info({_quote(index)})").fetchall():
            if _same(info[2], column):
                found.append(index)
                break
    return found


def _drop_column(conn: sqlite3.Connection, table: str, column: str) -> None:
    """删索引 → 删物理列 → 删元数据 → 清视图引用；不备份、不写审计。"""
    for index in _indexes_for_column(conn, table, column):
        # sqlite_autoindex_* 绑定 UNIQUE/PRIMARY KEY 约束，删不掉，交给下面兜底。
        if index.startswith("sqlite_"):
            continue
        conn.execute(f"DROP INDEX IF EXISTS {_quote(index)}")
    try:
        conn.execute(f"ALTER TABLE {_quote(table)} DROP COLUMN {_quote(column)}")
    except sqlite3.OperationalError as exc:
        raise ValueError(
            f"无法删除列「{column}」：该列可能仍被约束或表达式索引引用"
        ) from exc
    conn.execute(
        'DELETE FROM "__lsm_field" WHERE "table_name" = ? AND "column" = ?',
        (table, column),
    )
    for view_id, config in _view_entries(conn, table):
        if _pop_view_refs(config, column) is not None:
            _save_config(conn, view_id, config)


def _add_column(
    conn: sqlite3.Connection,
    table: str,
    name: str,
    type_: str,
    position: int,
    options: tuple,
    width: int,
) -> None:
    """建物理列 + 元数据行 + 索引；不写审计。"""
    conn.execute(
        f"ALTER TABLE {_quote(table)} ADD COLUMN {_quote(name)}"
        f" {schema.PHYSICAL_TYPES[type_]}"
    )
    conn.execute(
        'INSERT INTO "__lsm_field"'
        ' ("table_name", "column", "type", "options_json", "position", "width")'
        " VALUES (?, ?, ?, ?, ?, ?)",
        (table, name, type_, _options_json(options), position, width),
    )
    schema.ensure_indexes(conn, table)


# --- 公开接口 ---------------------------------------------------------


def add_field(
    conn: sqlite3.Connection,
    table: str,
    spec: schema.ColumnSpec,
    after: str | None = None,
    source: str = _SOURCE,
) -> int:
    """新增一个业务列，返回审计 op id；不提交事务。"""
    if not isinstance(spec, schema.ColumnSpec):
        raise ValueError("字段定义类型不正确")
    table = _ensure_managed(conn, table)
    type_ = _check_type(spec.type)
    options = _check_options(type_, spec.options)
    name = _validate_name(conn, table, spec.name)
    with _txn(conn):
        position = _insert_position(conn, table, after, _fields(conn, table))
        _add_column(
            conn, table, name, type_, position, options, schema.DEFAULT_FIELD_WIDTH
        )
        return audit.record(
            conn,
            "create_field",
            table,
            source,
            {
                "column": name,
                "type": type_,
                "options": list(options),
                "position": position,
            },
        )


def rename_field(
    conn: sqlite3.Connection, table: str, old: str, new: str, source: str = _SOURCE
) -> int:
    """给业务列改名，同步元数据、索引名与视图配置；返回 op id。不提交事务。"""
    table = _ensure_managed(conn, table)
    field = _find(_fields(conn, table), old)
    if field is None:
        raise ValueError(f"列不存在：{old}")
    if not isinstance(new, str) or not new.strip():
        raise ValueError("列名必须是非空文本")
    new = new.strip()
    if new == field.name:
        raise ValueError("新列名与原列名相同")
    if _same(new, field.name):
        raise ValueError("SQLite 列名不区分大小写，无法只修改大小写")
    new = _validate_name(conn, table, new, ignore=field.name)
    with _txn(conn):
        _rename_column(conn, table, field.name, new)
        return audit.record(
            conn, "rename_field", table, source, {"old": field.name, "new": new}
        )


def drop_field(
    conn: sqlite3.Connection, table: str, name: str, source: str = _SOURCE
) -> int:
    """删除业务列，值备份进 __lsm_trash_col；返回 op id。不提交事务。"""
    table = _ensure_managed(conn, table)
    field = _find(_fields(conn, table), name)
    if field is None:
        raise ValueError(f"列不存在：{name}")
    column = field.name
    backups = conn.execute(
        f'SELECT "id", {_quote(column)} FROM {_quote(table)}'
        f' WHERE {_quote(column)} IS NOT NULL ORDER BY "id"'
    ).fetchall()
    with _txn(conn):
        view_refs = _collect_view_refs(conn, table, column)
        op = audit.record(
            conn,
            "drop_field",
            table,
            source,
            {
                "column": column,
                "type": field.type,
                "options": list(field.options),
                "position": field.position,
                "backup_rows": len(backups),
                # 撤销回填需要列宽与视图引用，随 payload 一起带走。
                "width": field.width,
                "view_refs": view_refs,
            },
        )
        for row_id, value in backups:
            conn.execute(
                'INSERT INTO "__lsm_trash_col"'
                ' ("op_id", "table_name", "column", "type", "row_id", "value")'
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    op,
                    table,
                    column,
                    field.type,
                    row_id,
                    json.dumps(_encode_value(value), ensure_ascii=False),
                ),
            )
        _drop_column(conn, table, column)
    return op


def set_field_type(
    conn: sqlite3.Connection,
    table: str,
    name: str,
    type_: str,
    options=(),
    source: str = _SOURCE,
) -> int:
    """改逻辑类型与选项，只动 __lsm_field；返回 op id。不提交事务。"""
    table = _ensure_managed(conn, table)
    field = _find(_fields(conn, table), name)
    if field is None:
        raise ValueError(f"列不存在：{name}")
    type_ = _check_type(type_)
    options = _check_options(type_, options)
    with _txn(conn):
        conn.execute(
            'UPDATE "__lsm_field" SET "type" = ?, "options_json" = ?'
            ' WHERE "table_name" = ? AND "column" = ?',
            (type_, _options_json(options), table, field.name),
        )
        return audit.record(
            conn,
            "set_field_type",
            table,
            source,
            {
                "column": field.name,
                "from": field.type,
                "to": type_,
                "options": list(options),
                "old_options": list(field.options),
            },
        )


# --- 撤销处理器（导入即注册，重复导入幂等）----------------------------


def _payload(op: dict) -> dict:
    payload = op.get("payload")
    return payload if isinstance(payload, dict) else {}


def _live_table(conn: sqlite3.Connection, table) -> str:
    try:
        return schema._ensure_user_table(conn, table)
    except ValueError as exc:
        raise StaleOperation(str(exc)) from exc


def _later_ops_touch_field(
    conn: sqlite3.Connection, table: str, op_id: int, column: str
) -> bool:
    """op 之后是否还有未撤销的操作碰过这一列。

    两类来源：行级差分的 before/after 里带该列键（插入/更新/删除），以及
    字段元数据操作里指向该列（改名的新旧名、改类型/删列的目标列）。undo
    记录不算——撤销一条后续操作正是解除阻塞的方式。
    """
    rows = conn.execute(
        'SELECT "kind", "payload_json" FROM "__lsm_op"'
        ' WHERE "id" > ? AND "table_name" = ?'
        ' AND "undone_by" IS NULL AND "kind" <> ?',
        (op_id, table, "undo"),
    ).fetchall()
    for kind, raw in rows:
        payload = _load_json_object(raw)
        if payload is None:
            continue
        if kind == "rename_field":
            if _same(payload.get("old"), column) or _same(payload.get("new"), column):
                return True
        elif kind in ("create_field", "drop_field", "set_field_type"):
            if _same(payload.get("column"), column):
                return True
        # 行差分必须是列表；别的 kind 可能用 rows 存计数之类的标量，跳过。
        rows = payload.get("rows")
        if not isinstance(rows, list):
            continue
        for item in rows:
            if not isinstance(item, dict):
                continue
            for side in ("before", "after"):
                values = item.get(side)
                if isinstance(values, dict) and any(
                    _same(key, column) for key in values
                ):
                    return True
    return False


def _undo_create_field(conn: sqlite3.Connection, op: dict, source: str) -> dict:
    table = _live_table(conn, op.get("table"))
    column = _payload(op).get("column")
    if not column:
        raise StaleOperation("操作记录缺少列名")
    if _later_ops_touch_field(conn, table, op["id"], column):
        raise StaleOperation("该字段在此之后已被修改，请先撤销后续操作")
    if _find(_fields(conn, table), column) is None:
        raise StaleOperation(f"列已不存在：{column}")
    filled = conn.execute(
        f'SELECT COUNT(*) FROM {_quote(table)} WHERE {_quote(column)} IS NOT NULL'
    ).fetchone()[0]
    if filled:
        raise StaleOperation("该字段已有数据，请先清空再撤销")
    with _txn(conn):
        _drop_column(conn, table, column)
    return {"action": "drop_field", "column": column}


def _undo_rename_field(conn: sqlite3.Connection, op: dict, source: str) -> dict:
    table = _live_table(conn, op.get("table"))
    payload = _payload(op)
    old, new = payload.get("old"), payload.get("new")
    if not old or not new:
        raise StaleOperation("操作记录缺少列名")
    field = _find(_fields(conn, table), new)
    if field is None:
        raise StaleOperation(f"列已不存在：{new}")
    if _find(_fields(conn, table), old) is not None:
        raise StaleOperation(f"列名已被占用：{old}")
    try:
        target = _validate_name(conn, table, old, ignore=field.name)
    except ValueError as exc:
        raise StaleOperation(str(exc)) from exc
    with _txn(conn):
        _rename_column(conn, table, field.name, target)
    return {"action": "rename_field", "old": field.name, "new": target}


def _undo_drop_field(conn: sqlite3.Connection, op: dict, source: str) -> dict:
    table = _live_table(conn, op.get("table"))
    payload = _payload(op)
    column = payload.get("column")
    type_ = payload.get("type")
    if not column or type_ not in TYPES:
        raise StaleOperation("操作记录缺少字段定义")
    options = _check_options(type_, payload.get("options") or ())
    if _find(_fields(conn, table), column) is not None:
        raise StaleOperation(f"列名已被占用：{column}")
    try:
        name = _validate_name(conn, table, column)
    except ValueError as exc:
        raise StaleOperation(str(exc)) from exc
    # 只按 op_id 找备份：表若改过名，trash 行里的旧表名已经不可用，但行 id
    # 仍然有效，按 op 取才不会把值丢掉。
    backups = conn.execute(
        'SELECT "row_id", "value" FROM "__lsm_trash_col"'
        ' WHERE "op_id" = ? AND "column" = ? ORDER BY "row_id"',
        (op["id"], column),
    ).fetchall()
    position = payload.get("position")
    width = payload.get("width")
    if width is None:
        width = schema.DEFAULT_FIELD_WIDTH
    with _txn(conn):
        if isinstance(position, int):
            conn.execute(
                'UPDATE "__lsm_field" SET "position" = "position" + 1'
                ' WHERE "table_name" = ? AND "position" >= ?',
                (table, position),
            )
        else:
            position = max(
                (field.position for field in _fields(conn, table)), default=-1
            ) + 1
        _add_column(conn, table, name, type_, position, options, int(width))
        for row_id, raw in backups:
            value = None if raw is None else _decode_value(json.loads(raw))
            conn.execute(
                f'UPDATE {_quote(table)} SET {_quote(name)} = ? WHERE "id" = ?',
                (value, row_id),
            )
        _put_view_refs(conn, table, name, payload.get("view_refs") or [])
        # 回填过的备份立即回收，避免第二次撤销重复回填。
        conn.execute('DELETE FROM "__lsm_trash_col" WHERE "op_id" = ?', (op["id"],))
    return {"action": "create_field", "column": name, "position": position}


def _undo_set_field_type(conn: sqlite3.Connection, op: dict, source: str) -> dict:
    table = _live_table(conn, op.get("table"))
    payload = _payload(op)
    column = payload.get("column")
    to_type = payload.get("to")
    from_type = payload.get("from")
    if not column or from_type not in TYPES:
        raise StaleOperation("操作记录缺少字段类型")
    field = _find(_fields(conn, table), column)
    if field is None:
        raise StaleOperation(f"列已不存在：{column}")
    if field.type != to_type:
        raise StaleOperation("字段类型在此之后已被修改，请先撤销后续操作")
    options = _check_options(from_type, payload.get("old_options") or ())
    with _txn(conn):
        conn.execute(
            'UPDATE "__lsm_field" SET "type" = ?, "options_json" = ?'
            ' WHERE "table_name" = ? AND "column" = ?',
            (from_type, _options_json(options), table, field.name),
        )
    return {
        "action": "set_field_type",
        "column": column,
        "from": to_type,
        "to": from_type,
        "options": list(options),
    }


def _register_handlers() -> None:
    undo.register("create_field", _undo_create_field)
    undo.register("rename_field", _undo_rename_field)
    undo.register("drop_field", _undo_drop_field)
    undo.register("set_field_type", _undo_set_field_type)


_register_handlers()