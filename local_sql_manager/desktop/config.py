"""桌面端配置：数据目录、日志目录与 config.json。

配置存放在 %LOCALAPPDATA%\\LocalSQLManager\\config.json（可用 LSM_DATA_DIR 覆盖），
库文件默认放在「文档/LocalSQLManager」下，用户可见、可备份。
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from pathlib import Path

logger = logging.getLogger(__name__)

APP_DIR_NAME = "LocalSQLManager"
DEFAULT_PORT = 8765
DEFAULT_MCP_PORT = 8766


def data_dir() -> Path:
    """应用数据目录：配置、日志等内部文件。"""
    override = os.environ.get("LSM_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / APP_DIR_NAME
        return Path.home() / "AppData" / "Local" / APP_DIR_NAME
    base = os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / APP_DIR_NAME
    return Path.home() / ".local" / "share" / APP_DIR_NAME


def config_path() -> Path:
    """config.json 的完整路径。"""
    return data_dir() / "config.json"


def log_dir() -> Path:
    """日志目录，按天滚动的 app-YYYY-MM-DD.log 放在这里。"""
    return data_dir() / "logs"


def default_libraries_dir() -> Path:
    """库目录默认值：文档/LocalSQLManager。"""
    return Path.home() / "Documents" / APP_DIR_NAME


def new_mcp_token() -> str:
    """生成 MCP 访问 token；只在本机展示、写进 config.json。"""
    return secrets.token_urlsafe(24)


def default_config() -> dict:
    """首次运行或配置损坏时使用的默认配置。"""
    return {
        "libraries_dir": str(default_libraries_dir()),
        "port": DEFAULT_PORT,
        "mcp_port": DEFAULT_MCP_PORT,
        "mcp_read_only": False,
        "mcp_token": new_mcp_token(),
        "last_library": None,
    }


def _save_config(payload: dict) -> None:
    """把配置写回 config.json（先写临时文件再原子替换），失败不影响启动。"""
    path = config_path()
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("无法写入配置文件 %s：%s", path, exc)
        try:
            tmp.unlink()
        except OSError:
            pass


def _port_or_default(value, default: int) -> int:
    """端口必须是 0..65535 的整数，否则回退默认值。"""
    if isinstance(value, bool):
        return default
    try:
        port = int(value)
    except (TypeError, ValueError):
        return default
    if 0 <= port <= 65535:
        return port
    return default


def _bool_or_default(value, default: bool = False) -> bool:
    """宽松解析布尔配置，避免字符串 "false" 被当成 True。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True
        if text in ("false", "0", "no", "off", ""):
            return False
    return default


def _coerce(key: str, value):
    if key in ("port", "mcp_port"):
        return _port_or_default(value, default_config()[key])
    if key == "mcp_read_only":
        return _bool_or_default(value)
    if key == "mcp_token":
        text = value.strip() if isinstance(value, str) else ""
        # token 只走 HTTP 头/查询串，非 ASCII 会让比较与展示都出问题，按缺失处理。
        try:
            text.encode("ascii")
        except UnicodeEncodeError:
            return ""
        return text
    if key == "last_library":
        return str(value) if value else None
    return str(value)


def load_config() -> dict:
    """读取配置；缺失的键用默认值补齐，未知键丢弃。

    MCP token 首次运行时生成并写回 config.json；老配置缺这个键时补上再保存，
    这样重启后 token 不变，已粘贴到 MCP 客户端的配置继续可用。
    """
    config = default_config()
    path = config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _save_config(dict(config))
        return config
    except ValueError:
        # 文件内容解析不了：用默认配置继续跑并覆盖写回，否则每次启动都会
        # 轮换 token，用户已粘贴到 MCP 客户端的配置会静默失效。
        logger.warning("配置文件无法解析，已按默认配置重写，MCP token 已轮换")
        _save_config(dict(config))
        return config
    except OSError as exc:
        # 读不了（权限、被占用）：不覆盖用户文件，但要说明 token 无法保持。
        logger.warning("无法读取配置文件，MCP token 本次启动会重新生成：%s", exc)
        return config
    if not isinstance(raw, dict):
        logger.warning("配置文件不是对象，已按默认配置重写，MCP token 已轮换")
        _save_config(dict(config))
        return config
    for key in config:
        if key in raw:
            config[key] = _coerce(key, raw[key])
    if not config["mcp_token"]:
        config["mcp_token"] = new_mcp_token()
    if raw.get("mcp_token") != config["mcp_token"]:
        raw["mcp_token"] = config["mcp_token"]
        _save_config(raw)
    return config
