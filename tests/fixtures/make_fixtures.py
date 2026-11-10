"""测试用工作簿生成器。

工作簿在测试时现场生成，仓库里不提交二进制文件。
"""

from __future__ import annotations

import csv
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

def make_csv(path):
    """单 sheet 的 CSV：前导零列与数字列。"""
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["名称", "金额", "编码"])
        writer.writerow(["张三", "12.5", "007"])
        writer.writerow(["李四", "20", "012"])
    return path


def make_csv_bom_dup(path):
    """带 BOM 且第 1、2 列同名的 CSV。"""
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["金额", "金额", "名称"])
        writer.writerow(["10", "20", "测试"])
    return path

def make_header_only_workbook(path):
    """只有表头、没有数据行的 sheet。"""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "只有表头"
    sheet.append(["名称", "金额"])
    workbook.save(path)
    return path


def make_id_header_workbook(path):
    """表头撞上受管表的内部列名 id。"""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "编号表"
    sheet.append(["id", "名称"])
    sheet.append(["7", "甲"])
    workbook.save(path)
    return path


def make_collision_workbook(path):
    """两个 sheet 清洗后同名（「订单」与尾随空格的「订单 」）。"""
    workbook = Workbook()
    first = workbook.active
    first.title = "订单"
    first.append(["值"])
    first.append(["a"])
    second = workbook.create_sheet("订单 ")
    second.append(["值"])
    second.append(["b"])
    workbook.save(path)
    return path


def make_dirty_workbook(path):
    """第 2 个数据行的金额是 N/A，强制 number 时会失败。"""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "脏值"
    sheet.append(["名称", "金额"])
    sheet.append(["甲", "1"])
    sheet.append(["乙", "N/A"])
    workbook.save(path)
    return path


def make_csv_gbk(path):
    """中文 Windows 上 Excel 另存的 ANSI/GBK 编码 CSV。"""
    with open(path, "w", encoding="gbk", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["名称", "金额"])
        writer.writerow(["张三", "10"])
    return path


def make_csv_nul(path):
    """数据行里含 NUL 字节，csv 模块会拒绝。"""
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("名称,金额\n")
        handle.write("甲,1\n")
        handle.write("乙,\x00\n")
    return path


def make_csv_long_header(path):
    """表头超过 64 字符的 CSV。"""
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["c" * 65, "值"])
        writer.writerow(["x", "1"])
    return path
