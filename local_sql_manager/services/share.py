"""分享包（.lsmf）的导出与导入。

分享包是一个 zip，只含两个条目：`manifest.json` 与 `db.sqlite`。
导入只做校验、解压与改名，成功后由调用方作为**新库**打开（不合并）。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from .library import open_library
from .models import ShareError, UnsupportedFormat
from .util import utcnow_iso

# 分享包自身的格式版本，与库格式版本相互独立。
FORMAT_VERSION = 1

MANIFEST_NAME = "manifest.json"
DB_NAME = "db.sqlite"
ENTRY_NAMES = (MANIFEST_NAME, DB_NAME)

# 解压总量与压缩比阈值：正常库远低于这两个上限，异常包在此拦下。
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
# 小包不查压缩比：空的 SQLite 页几乎全是零字节，几 KB 的合法库压缩比也能过百，
# 而这么小的包本身没有危害。
_RATIO_CHECK_MIN_BYTES = 16 * 1024 * 1024

_SQLITE_MAGIC = b"SQLite format 3\x00"

_COPY_CHUNK = 1024 * 1024


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _table_summary(conn: sqlite3.Connection) -> list:
    """列出用户表及行数；内部表（__lsm_ 与 sqlite_ 前缀）不写进 manifest。"""
    names = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        if not str(row[0]).startswith(("__lsm_", "sqlite_"))
    ]
    summary = []
    for name in names:
        count = conn.execute(f"SELECT COUNT(*) FROM {_quote(name)}").fetchone()[0]
        summary.append({"name": name, "row_count": count})
    return summary


def export_lsmf(conn: sqlite3.Connection, db_path, out_path, app_version: str):
    """把库打成分享包，返回包路径。

    `conn` 用 SQLite 的 backup 接口复制出一致快照，因此即使库处于 WAL 模式也不用
    手工拷贝文件；源库完全不受影响。审计与回收站属于库的完整状态的一部分，一并入包，
    接收方才能看到改动的来龙去脉。
    """
    out = Path(out_path) if out_path is not None else Path(db_path).with_suffix(".lsmf")
    if out.resolve() == Path(db_path).resolve():
        raise ShareError("导出路径不能与库文件相同")
    out.parent.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix=".lsm-export-", dir=str(out.parent)))
    try:
        snapshot = workdir / DB_NAME
        copy = sqlite3.connect(str(snapshot))
        try:
            conn.backup(copy)
            manifest = {
                "format": FORMAT_VERSION,
                "app_version": app_version,
                "exported_at": utcnow_iso(),
                "tables": _table_summary(copy),
            }
        finally:
            copy.close()
        package = workdir / "package.lsmf"
        with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2)
            )
            zf.write(snapshot, DB_NAME)
        os.replace(package, out)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return out


def _check_entries(infos: list) -> None:
    names = [info.filename for info in infos]
    if len(names) != len(ENTRY_NAMES) or set(names) != set(ENTRY_NAMES):
        raise ShareError(
            "分享包内容不符合预期：只允许 manifest.json 与 db.sqlite"
        )
    for info in infos:
        name = info.filename
        if info.is_dir() or "\\" in name or name.startswith("/"):
            raise ShareError(f"分享包包含非法条目：{name}")
        parts = PurePosixPath(name).parts
        if len(parts) != 1 or ".." in parts:
            raise ShareError(f"分享包包含非法路径：{name}")
        if info.flag_bits & 0x1:
            raise ShareError("分享包已加密，无法导入")


def _check_sizes(infos: list) -> None:
    total = 0
    packed = 0
    for info in infos:
        if info.file_size < 0 or info.compress_size < 0:
            raise ShareError("分享包条目大小异常")
        total += info.file_size
        packed += info.compress_size
    if total > MAX_UNCOMPRESSED_BYTES:
        raise ShareError(
            f"分享包解压后过大（{total} 字节，上限 {MAX_UNCOMPRESSED_BYTES} 字节）"
        )
    if (
        total > _RATIO_CHECK_MIN_BYTES
        and packed
        and total > packed * MAX_COMPRESSION_RATIO
    ):
        raise ShareError("分享包压缩比异常，已拒绝导入")


def _read_manifest(raw: bytes) -> dict:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShareError(f"manifest.json 无法解析：{exc}") from None
    if not isinstance(data, dict):
        raise ShareError("manifest.json 结构不正确")
    return data


def _check_format(manifest: dict) -> None:
    fmt = manifest.get("format")
    if isinstance(fmt, bool) or not isinstance(fmt, int) or fmt < 1:
        raise ShareError(f"manifest.json 缺少有效的格式版本：{fmt!r}")
    if fmt > FORMAT_VERSION:
        raise ShareError(
            f"分享包格式版本 {fmt} 高于当前应用支持的 {FORMAT_VERSION}，请升级后再导入"
        )


def _extract_db(zf: zipfile.ZipFile, info: zipfile.ZipInfo, target: Path) -> None:
    """流式解压并在超过总量上限时中止，防止条目头谎报大小。"""
    written = 0
    with zf.open(info) as reader, open(target, "wb") as writer:
        while True:
            chunk = reader.read(_COPY_CHUNK)
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_UNCOMPRESSED_BYTES:
                raise ShareError("分享包解压后过大，已中止导入")
            writer.write(chunk)


def _validate_database(path: Path) -> None:
    with open(path, "rb") as handle:
        header = handle.read(len(_SQLITE_MAGIC))
    if header != _SQLITE_MAGIC:
        raise ShareError("分享包内的 db.sqlite 不是有效的 SQLite 数据库")
    try:
        library = open_library(str(path))
    except UnsupportedFormat as exc:
        raise ShareError(str(exc)) from None
    except sqlite3.DatabaseError as exc:
        raise ShareError(f"分享包内的库文件无法打开：{exc}") from None
    library.conn.close()


def _unique_destination(directory: Path, stem: str) -> Path:
    candidate = directory / f"{stem}.sqlite"
    index = 2
    while candidate.exists():
        candidate = directory / f"{stem}-{index}.sqlite"
        index += 1
    return candidate


def import_lsmf(path, libraries_dir):
    """校验并导入分享包，返回新库路径；同名库自动加序号。"""
    source = Path(path)
    directory = Path(libraries_dir)
    try:
        zf = zipfile.ZipFile(source)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ShareError(f"分享包无法打开：{exc}") from None

    with zf:
        infos = zf.infolist()
        _check_entries(infos)
        _check_sizes(infos)
        _check_format(_read_manifest(zf.read(MANIFEST_NAME)))
        info = next(item for item in infos if item.filename == DB_NAME)

        directory.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix=".tmp-", dir=str(directory)))
        try:
            extracted = workdir / DB_NAME
            _extract_db(zf, info, extracted)
            _validate_database(extracted)
            destination = _unique_destination(directory, source.stem)
            os.replace(extracted, destination)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
    return destination
