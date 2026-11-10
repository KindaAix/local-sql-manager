"""服务层共享的数据模型与异常。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Field:
    """一个逻辑字段。"""

    name: str
    type: str
    options: tuple = ()
    position: int = 0
    width: int = 140


@dataclass
class Group:
    """分组头：分组值、组内总条数与汇总结果。"""

    key: Any
    count: int
    aggregates: tuple = ()


@dataclass
class PageResult:
    """一页数据及其分页信息。"""

    rows: list
    total: int
    groups: tuple = ()
    page: int = 1
    page_size: int = 100


@dataclass
class ViewConfig:
    """视图配置，对应 __lsm_view.config_json。"""

    filters: list = field(default_factory=list)
    sorts: list = field(default_factory=list)
    hidden: list = field(default_factory=list)
    order: list = field(default_factory=list)
    widths: dict = field(default_factory=dict)
    group: dict | None = None
    aggregates: list = field(default_factory=list)


class ConflictError(Exception):
    """乐观锁冲突，rows 保存冲突行的最新值。"""

    def __init__(self, rows: list):
        super().__init__("该记录已被修改")
        self.rows = rows


class StaleOperation(Exception):
    """要撤销的操作已被后续写入影响，不能再回退。"""


class Irreversible(Exception):
    """该操作不可撤销（例如彻底删除）。"""


class ShareError(Exception):
    """分享包校验或导入失败。"""


class AdoptError(Exception):
    """外部表不满足启用编辑的前置条件。"""


class UnsupportedFormat(Exception):
    """库或分享包的格式版本高于当前应用。"""
