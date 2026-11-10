"""测试用工作簿生成器。

工作簿在测试时现场生成，仓库里不提交二进制文件。
"""

from __future__ import annotations

from datetime import date, datetime

from openpyxl import Workbook


def make_workbook(path):
    """3 个 sheet：订单（混合类型、前导零、日期与时间）、空表、客户。"""
    workbook = Workbook()
    orders = workbook.active
    orders.title = "订单"
    orders.append(["编码", "金额", "下单日期", "备注", "创建时间"])
    orders.append(["007", 10.5, date(2024, 1, 1), "加急", datetime(2024, 1, 1, 9, 30)])
    orders.append(["012", 20, date(2024, 1, 2), 3, datetime(2024, 1, 2, 10, 0)])

    workbook.create_sheet("空表")

    customers = workbook.create_sheet("客户")
    customers.append(["名称", "电话"])
    customers.append(["张三", "013800000000"])
    customers.append(["李四", "014900000000"])

    workbook.save(path)
    return path


def make_dup_workbook(path):
    """一个 sheet，第 5、6 列都叫「金额」，用于验证重名列消歧。"""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "重复"
    sheet.append(["编号", "名称", "日期", "状态", "金额", "金额"])
    sheet.append(["A-1", "测试", "2024-01-01", "新建", 10, 20])
    workbook.save(path)
    return path


def make_bad_workbook(path):
    """只有「订单」一个 sheet；配合选中「客户」制造导入中途失败。"""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "订单"
    sheet.append(["编码", "金额"])
    sheet.append(["001", 1])
    workbook.save(path)
    return path


def make_large_workbook(path, count=600):
    """超过一个批次（500 行）的工作簿，用于验证分批写入。"""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "大表"
    sheet.append(["序号", "名称"])
    for index in range(count):
        sheet.append([index + 1, f"名称{index + 1}"])
    workbook.save(path)
    return path


def make_reserved_workbook(path):
    """sheet 名撞上内部表前缀 __lsm_。"""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "__lsm_秘密"
    sheet.append(["值"])
    sheet.append(["x"])
    workbook.save(path)
    return path
