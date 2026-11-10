"""逻辑字段类型、类型推断与取值转换。"""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime
from typing import Sequence

# 逻辑字段类型，见设计文档 §6。
TYPES = ("text", "number", "date", "checkbox", "select", "multiselect", "url")

_CAST_TYPES = {
    "text": "TEXT",
    "number": "REAL",
    "date": "TEXT",
    "checkbox": "INTEGER",
    "select": "TEXT",
    "multiselect": "TEXT",
    "url": "TEXT",
}

_CHECKBOX_VALUES = {"是", "否", "true", "false"}

_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d")

_PURE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _is_number(text: str) -> bool:
    try:
        return math.isfinite(float(text))
    except ValueError:
        return False


def _parse_date(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _has_leading_zero(text: str) -> bool:
    body = text.lstrip("+-")
    return len(body) > 1 and body[0] == "0" and body[1:].isdigit()


def _is_pure_date(text: str) -> bool:
    """仅把 YYYY-MM-DD 形式的纯日期视为日期列。"""
    return bool(_PURE_DATE_RE.match(text)) and _parse_date(text) is not None


def _is_checkbox(text: str) -> bool:
    return text.lower() in _CHECKBOX_VALUES


def infer_type(values: Sequence[str]) -> tuple[str, bool]:
    """推断一列的逻辑类型，返回 (类型, 是否可疑)。

    空字符串与 None 不参与推断；类型混杂时回退为 text 并标记可疑。
    """
    cleaned = [str(value).strip() for value in values if value is not None]
    cleaned = [value for value in cleaned if value]
    if not cleaned:
        return ("text", False)
    if any(_has_leading_zero(value) for value in cleaned):
        return ("text", False)
    if all(_is_number(value) for value in cleaned):
        return ("number", False)
    if all(_is_pure_date(value) for value in cleaned):
        return ("date", False)
    if all(_is_checkbox(value) for value in cleaned):
        return ("checkbox", False)
    if any(
        _is_number(value) or _is_pure_date(value) or _is_checkbox(value)
        for value in cleaned
    ):
        return ("text", True)
    return ("text", False)


def cast_expr(column: str, type_: str) -> str:
    """按逻辑类型生成读取用的 CAST 表达式。"""
    sql_type = _CAST_TYPES.get(type_, "TEXT")
    return f"CAST({_quote_ident(column)} AS {sql_type})"


def coerce(value: object, type_: str) -> object:
    """把外部输入的取值转换成适合入库的形式；空值统一为 None。"""
    if value is None:
        return None
    if type_ == "number":
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return value
        text = str(value).strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return float(text)
    if type_ == "checkbox":
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int) and value in (0, 1):
            return value
        text = str(value).strip().lower()
        if not text:
            return None
        if text in ("是", "true", "1"):
            return 1
        if text in ("否", "false", "0"):
            return 0
        raise ValueError(f"无法转换为勾选值: {value!r}")
    if type_ == "multiselect":
        if isinstance(value, (list, tuple)):
            return json.dumps(list(value), ensure_ascii=False)
        text = str(value).strip()
        return text or None
    if type_ == "date":
        text = str(value).strip()
        if not text:
            return None
        parsed = _parse_date(text)
        if parsed is None:
            raise ValueError(f"无法转换为日期: {value!r}")
        return parsed.isoformat()
    text = str(value).strip()
    return text or None
