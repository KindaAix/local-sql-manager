"""导入向导、导出与分享包：页面与下载入口。

本模块只负责把表单/查询参数转成服务层调用，并管理上传临时文件的生命周期：

- 上传文件先落到系统临时目录（临时目录名前缀 lsm-import-），绝不写进库目录；
- 预览状态以 token 关联，确认、取消、过期（30 分钟）都会删除临时目录；
- 写操作成功 commit、失败 rollback，错误一律换成中文提示。

服务层函数均不提交事务，路由负责事务边界。
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
from pathlib import Path

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from local_sql_manager.app import get_conn
from local_sql_manager.services import export_excel, schema, share, views
from local_sql_manager.services.import_excel import (
    MAX_NAME_LENGTH,
    _sanitize_table_name,
    import_sheets,
    parse_workbook,
)
from local_sql_manager.services.models import ShareError, ViewConfig
from local_sql_manager.services.types import TYPES

logger = logging.getLogger(__name__)

bp = Blueprint("import_export", __name__)

ALLOWED_SUFFIXES = (".xlsx", ".csv")
CONFLICT_MODES = ("overwrite", "copy", "skip")
CONFLICT_LABELS = {"overwrite": "覆盖同名表", "copy": "建副本", "skip": "跳过同名表"}
TYPE_LABELS = {
    "text": "文本",
    "number": "数字",
    "date": "日期",
    "checkbox": "勾选",
    "select": "单选",
    "multiselect": "多选",
    "url": "链接",
}

# 预览状态在内存里保留 30 分钟；确认/取消后立即清理。
UPLOAD_TTL_SECONDS = 30 * 60

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CSV_MIME = "text/csv"

_lock = threading.Lock()
_pending: dict[str, dict] = {}


# --- 上传状态 ---------------------------------------------------------


def _sweep_locked() -> None:
    """清掉过期上传；调用方必须已经持有 _lock。"""
    now = time.time()
    expired = [
        token
        for token, state in _pending.items()
        if now - state["created"] > UPLOAD_TTL_SECONDS
    ]
    for token in expired:
        state = _pending.pop(token)
        shutil.rmtree(state["dir"], ignore_errors=True)


def _pending_state(token: str) -> dict | None:
    with _lock:
        _sweep_locked()
        return _pending.get(token)


def _pending_remember(path: Path, directory: Path, filename: str, previews: list) -> str:
    token = uuid.uuid4().hex
    with _lock:
        _sweep_locked()
        _pending[token] = {
            "path": path,
            "dir": directory,
            "name": filename,
            "previews": previews,
            "created": time.time(),
        }
    return token


def _pending_drop(token: str | None) -> None:
    if not token:
        return
    with _lock:
        state = _pending.pop(token, None)
    if state is not None:
        shutil.rmtree(state["dir"], ignore_errors=True)


def _safe_filename(raw: str) -> str:
    """只保留文件名本身；中文名原样保留（CSV 的表名取自文件 stem）。"""
    name = str(raw or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name or name in (".", ".."):
        return "upload"
    stem, dot, suffix = name.rpartition(".")
    if dot and len(stem) + len(suffix) > 120:
        return stem[: 120 - len(suffix) - 1] + "." + suffix
    return name


def _parse_int(raw: object) -> int | None:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


# --- 导入向导 ---------------------------------------------------------


@bp.app_errorhandler(413)
def _upload_too_large(_error):
    return (
        render_template(
            "import.html",
            error="文件超过 20MB 上限，请拆分后再导入。",
        ),
        413,
    )


@bp.get("/import")
def import_page():
    return render_template("import.html", error=None)


def _preview_context(state: dict, token: str, **extra) -> dict:
    previews = state["previews"]
    items = []
    for index, preview in enumerate(previews):
        columns = [
            {
                "index": column_index,
                "name": name,
                "type": preview.types.get(name, "text"),
                "suspicious": name in preview.suspicious,
            }
            for column_index, name in enumerate(preview.columns)
        ]
        items.append(
            {
                "index": index,
                "name": preview.name,
                "table_name": _sanitize_table_name(preview.name),
                "row_count": preview.row_count,
                "sample": preview.sample,
                "columns": columns,
                "empty": preview.empty,
                "sampled": preview.sampled,
                "suspicious": list(preview.suspicious),
            }
        )
    return {
        "token": token,
        "sheets": items,
        "file_name": state["name"],
        "types": TYPE_LABELS,
        "conflict_modes": CONFLICT_LABELS,
        "error": None,
        **extra,
    }


@bp.post("/import")
def import_upload():
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        flash("请选择要导入的 Excel 或 CSV 文件。")
        return redirect(url_for("import_export.import_page"))

    filename = _safe_filename(upload.filename)
    suffix = Path(filename).suffix.lower()
    if suffix == ".xls":
        flash("暂不支持旧版 .xls，请在 Excel 中另存为 .xlsx 后再上传。")
        return redirect(url_for("import_export.import_page"))
    if suffix not in ALLOWED_SUFFIXES:
        flash("只支持 .xlsx 与 .csv 文件。")
        return redirect(url_for("import_export.import_page"))

    directory = Path(tempfile.mkdtemp(prefix="lsm-import-"))
    path = directory / filename
    try:
        upload.save(path)
        try:
            previews = parse_workbook(path)
        except Exception:  # noqa: BLE001 文件内容不可信，统一按“无法解析”处理
            logger.exception("导入文件解析失败")
            shutil.rmtree(directory, ignore_errors=True)
            flash("无法解析该文件，请确认是有效的 .xlsx 或 .csv。")
            return redirect(url_for("import_export.import_page"))
        if not previews:
            shutil.rmtree(directory, ignore_errors=True)
            flash("文件里没有可导入的工作表。")
            return redirect(url_for("import_export.import_page"))
    except OSError:
        shutil.rmtree(directory, ignore_errors=True)
        logger.exception("保存上传文件失败")
        flash("保存上传文件失败，请重试。")
        return redirect(url_for("import_export.import_page"))

    token = _pending_remember(path, directory, filename, previews)
    state = _pending_state(token)
    return render_template("import_preview.html", **_preview_context(state, token))


def _form_choices(previews: list) -> dict:
    """把确认表单里的勾选、目标表名与类型覆盖整理成结构。"""
    selected: list[int] = []
    names: dict[int, str] = {}
    types: dict[int, dict[str, str]] = {}
    for index, preview in enumerate(previews):
        if f"sheet_{index}" not in request.form:
            continue
        selected.append(index)
        raw_name = (request.form.get(f"name_{index}") or "").strip()
        names[index] = raw_name or _sanitize_table_name(preview.name)
        overrides = {}
        for column_index, column in enumerate(preview.columns):
            value = request.form.get(f"type_{index}_{column_index}")
            if value in TYPES:
                overrides[column] = value
        types[index] = overrides
    return {"selected": selected, "names": names, "types": types}


def _validate_names(previews: list, choices: dict) -> None:
    seen: set[str] = set()
    for index in choices["selected"]:
        name = choices["names"][index]
        if not name:
            raise ValueError("目标表名不能为空")
        if len(name) > MAX_NAME_LENGTH:
            raise ValueError(f"目标表名不能超过 {MAX_NAME_LENGTH} 个字符：{name}")
        if name.lower().startswith(("__lsm_", "sqlite_")):
            raise ValueError(f"目标表名不能以 __lsm_ 或 sqlite_ 开头：{name}")
        key = name.lower()
        if key in seen:
            raise ValueError(f"目标表名重复：{name}")
        seen.add(key)


def _existing_object_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0]).lower()
        for row in conn.execute("SELECT name FROM sqlite_master")
    }


def _planned_targets(
    conn: sqlite3.Connection, previews: list, choices: dict, on_conflict: str
) -> list[tuple[str, str]]:
    """按 import_sheets 的处理顺序，预先算好「导入后的表名 → 目标表名」。

    import_sheets 只在 sheet 为空或 skip 撞名时才少导一张，这里按同样规则
    预测，确认返回的 created 列表与预期数量一致，避免把表改错名字。
    """
    existing = _existing_object_names(conn)
    planned = []
    for index in choices["selected"]:
        preview = previews[index]
        if preview.empty or not preview.columns:
            continue
        base = _sanitize_table_name(preview.name)
        if on_conflict == "skip" and base.lower() in existing:
            continue
        planned.append((base, choices["names"][index]))
    return planned


@bp.post("/import/confirm")
def import_confirm():
    token = request.form.get("token") or ""
    state = _pending_state(token)
    if state is None:
        flash("上传已过期，请重新选择文件。")
        return redirect(url_for("import_export.import_page"))

    previews = state["previews"]
    on_conflict = request.form.get("on_conflict") or "skip"
    if on_conflict not in CONFLICT_MODES:
        on_conflict = "skip"

    choices = _form_choices(previews)
    if not choices["selected"]:
        _pending_drop(token)
        flash("请至少选择一个工作表。")
        return redirect(url_for("import_export.import_page"))

    conn = get_conn()
    try:
        _validate_names(previews, choices)
        planned = _planned_targets(conn, previews, choices, on_conflict)
        if not planned:
            raise ValueError("选中的工作表都是空的或已跳过，没有可导入的内容")
        selected_sheets = [previews[index].name for index in choices["selected"]]
        overrides = {
            previews[index].name: mapping
            for index, mapping in choices["types"].items()
            if mapping
        }
        created = import_sheets(
            conn,
            state["path"],
            selected_sheets,
            overrides,
            on_conflict,
        )
        if len(created) != len(planned):
            raise ValueError("导入结果与预览不一致，请重新上传文件")
        final_names = []
        for created_name, (base, wanted) in zip(created, planned):
            # 建副本时 created 可能已经是 base_2；用户没改目标名就保留副本名。
            if wanted != base and wanted != created_name:
                schema.rename_table(conn, created_name, wanted)
                final_names.append(wanted)
            else:
                final_names.append(created_name)
        conn.commit()
    except Exception as exc:  # noqa: BLE001 统一映射成中文提示
        conn.rollback()
        logger.exception("导入失败")
        message = _import_error(exc)
        context = _preview_context(
            state,
            token,
            error=message,
            choices=choices,
            chosen_conflict=on_conflict,
        )
        return render_template("import_preview.html", **context), 400
    _pending_drop(token)
    flash(f"已导入 {len(final_names)} 张工作表。")
    return redirect(url_for("grid.table_view", table=final_names[0]))


def _import_error(exc: BaseException) -> str:
    if isinstance(exc, ValueError):
        return str(exc) or "导入参数不正确"
    if isinstance(exc, sqlite3.Error):
        return "写入数据库失败，请重试"
    if isinstance(exc, OSError):
        return "读取上传文件失败，请重新上传"
    return "导入失败，请重试"


@bp.post("/import/cancel")
def import_cancel():
    _pending_drop(request.form.get("token"))
    flash("已取消导入。")
    return redirect(url_for("import_export.import_page"))


# --- 导出 -------------------------------------------------------------


def _view_config(conn: sqlite3.Connection, table: str, raw: object) -> ViewConfig:
    """按 view 参数取配置；参数非法或属于别的表时用默认视图。"""
    view_id = _parse_int(raw)
    if view_id is not None:
        row = views._fetch(conn, view_id)
        if row is not None and row[1] == table:
            return views.load_config(conn, view_id)
    return ViewConfig()


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _csv_columns(conn: sqlite3.Connection, table: str, config: ViewConfig) -> list[str]:
    fields = schema.get_fields(conn, table)
    if not fields:
        raise ValueError("该表尚未启用编辑，无法导出")
    existing = [
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({_quote(table)})")
    ]
    return export_excel._export_columns(fields, config, existing)


def _send_download(path: Path, directory: Path, name: str, mimetype: str):
    """发文件，并在响应关闭后删除整个临时目录（Windows 上文件需先关闭）。"""
    response = send_file(
        path,
        as_attachment=True,
        download_name=name,
        mimetype=mimetype,
    )
    response.call_on_close(lambda: shutil.rmtree(directory, ignore_errors=True))
    return response


@bp.get("/export/<path:table>")
def export_table(table: str):
    conn = get_conn()
    if table not in schema.list_tables(conn):
        flash(f"表「{table}」不存在，无法导出。")
        return redirect(url_for("grid.index"))
    fmt = (request.args.get("format") or "xlsx").lower()
    if fmt not in ("xlsx", "csv"):
        flash("导出格式只支持 xlsx 或 csv。")
        return redirect(url_for("grid.table_view", table=table))

    directory = Path(tempfile.mkdtemp(prefix="lsm-export-"))
    path = directory / f"export.{fmt}"
    try:
        config = _view_config(conn, table, request.args.get("view"))
        if fmt == "xlsx":
            export_excel.export_view(conn, table, config, path)
        else:
            export_excel.export_csv(
                conn, table, _csv_columns(conn, table, config), path
            )
    except (ValueError, sqlite3.Error, OSError) as exc:
        shutil.rmtree(directory, ignore_errors=True)
        flash(str(exc) if isinstance(exc, ValueError) else "导出失败，请重试。")
        return redirect(url_for("grid.table_view", table=table))

    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"{table}-{stamp}.{fmt}"
    return _send_download(path, directory, name, XLSX_MIME if fmt == "xlsx" else CSV_MIME)


# --- 分享包 -----------------------------------------------------------


@bp.get("/share/export/<path:table>")
def share_export(table: str):
    conn = get_conn()
    if table not in schema.list_tables(conn):
        flash(f"表「{table}」不存在，无法分享。")
        return redirect(url_for("grid.index"))
    db_path = current_app.config.get("LSM_DB_PATH")
    if not db_path:
        flash("未绑定库文件，无法导出分享包。")
        return redirect(url_for("grid.index"))

    directory = Path(tempfile.mkdtemp(prefix="lsm-share-"))
    path = directory / "export.lsmf"
    try:
        share.export_lsmf(conn, db_path, path, _app_version())
    except (ShareError, OSError, sqlite3.Error) as exc:
        shutil.rmtree(directory, ignore_errors=True)
        flash(str(exc) if isinstance(exc, ShareError) else "导出分享包失败，请重试。")
        return redirect(url_for("grid.table_view", table=table))

    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"{Path(db_path).stem}-{stamp}.lsmf"
    return _send_download(path, directory, name, "application/zip")


def _app_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("local-sql-manager")
    except Exception:  # noqa: BLE001 未安装元数据时用占位版本
        return "0.0.0"


@bp.post("/share/import")
def share_import():
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        flash("请选择要导入的 .lsmf 分享包。")
        return redirect(url_for("grid.index"))
    filename = _safe_filename(upload.filename)
    if Path(filename).suffix.lower() != ".lsmf":
        flash("只支持 .lsmf 分享包。")
        return redirect(url_for("grid.index"))

    db_path = current_app.config.get("LSM_DB_PATH")
    if not db_path:
        flash("未绑定库文件，无法导入分享包。")
        return redirect(url_for("grid.index"))

    directory = Path(tempfile.mkdtemp(prefix="lsm-share-"))
    try:
        path = directory / filename
        upload.save(path)
        new_path = share.import_lsmf(path, Path(db_path).parent)
    except ShareError as exc:
        flash(str(exc) or "分享包无效，无法导入。")
        return redirect(url_for("grid.index"))
    except OSError:
        logger.exception("导入分享包失败")
        flash("导入分享包失败，请重试。")
        return redirect(url_for("grid.index"))
    finally:
        shutil.rmtree(directory, ignore_errors=True)

    flash(
        f"分享包已导入为新库「{new_path.name}」，位于库目录；"
        "重启应用后可在库列表中打开。"
    )
    return redirect(url_for("grid.index"))
