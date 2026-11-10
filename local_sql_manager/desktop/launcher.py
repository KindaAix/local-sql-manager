"""桌面启动入口：单实例、日志、本地服务、托盘。

启动顺序（设计文档 §5）：单实例 → 日志 → 读配置 → 开库 → 起 Flask
→ 起 MCP → 开浏览器 → 托盘阻塞。退出时先停 MCP、再停 Web、最后释放
单实例锁。
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import time
import webbrowser
from functools import partial
from pathlib import Path

from flask import Flask
from werkzeug.serving import make_server

from local_sql_manager.app import create_app
from local_sql_manager.services.library import Library, open_library

from .config import data_dir, load_config, log_dir
from .logging_setup import setup_logging
from .tray import run_tray

logger = logging.getLogger(__name__)

APP_MUTEX = "LocalSQLManager.SingleInstance"
HOST = "127.0.0.1"

# name -> 持有的句柄（Windows 互斥体句柄或 POSIX 文件对象）
_handles: dict[str, object] = {}


def _windows_kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    return kernel32


def acquire_single_instance(name: str) -> bool:
    """获取命名单实例锁；已在运行则返回 False。"""
    if os.name == "nt":
        kernel32 = _windows_kernel32()
        ERROR_ALREADY_EXISTS = 183
        handle = kernel32.CreateMutexW(None, 1, name)
        if not handle:
            return False
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        previous = _handles.pop(name, None)
        if previous is not None:
            kernel32.CloseHandle(previous)
        _handles[name] = handle
        return True

    # 非 Windows：同一把 flock 对不同打开描述符互斥，便于在同一进程内测试。
    try:
        import fcntl

        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
        path = Path(tempfile.gettempdir()) / f"lsm-{digest}.lock"
        stream = path.open("a+")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return False
        _handles[name] = stream
        return True
    except (ImportError, OSError):
        return False


def release_single_instance(name: str) -> None:
    """释放命名锁；未持有时什么也不做。"""
    handle = _handles.pop(name, None)
    if handle is None:
        return
    if os.name == "nt":
        _windows_kernel32().CloseHandle(handle)
        return
    try:
        handle.close()  # type: ignore[attr-defined]
    except OSError:
        pass


def pick_port(preferred: int) -> int:
    """优先使用 preferred；被占用或越界时让系统分配一个空闲端口。"""
    if isinstance(preferred, bool):
        preferred = 0
    else:
        try:
            preferred = int(preferred)
        except (TypeError, ValueError):
            preferred = 0
    if not 0 <= preferred <= 65535:
        preferred = 0
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((HOST, preferred))
        except OSError:
            pass
        else:
            return probe.getsockname()[1]
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        return probe.getsockname()[1]


def library_path(config: dict) -> str:
    """决定启动时打开的库：上次使用的库，否则库目录下的默认库。"""
    last = config.get("last_library")
    if last:
        return str(last)
    return str(Path(config["libraries_dir"]) / "我的数据.sqlite")


class McpHandle:
    """MCP 服务的句柄：保存 uvicorn 服务与线程，负责停止并暴露实际端口。"""

    def __init__(self, server, thread: threading.Thread, port: int):
        self.server = server
        self.thread = thread
        self.port = port

    def stop(self) -> None:
        """通知 uvicorn 退出并等待线程结束；重复调用无害。"""
        self.server.should_exit = True
        if self.thread.is_alive():
            self.thread.join(timeout=5)


def start_mcp(
    library: Library, *, port: int, read_only: bool, token: str | None = None
):
    """在 daemon 线程里跑 FastMCP 的 streamable-http 服务。

    token 交给 build_mcp，再由 ASGI 中间件校验；返回 McpHandle；依赖缺失或
    启动失败时记日志并返回 None，不阻断桌面壳。
    """
    try:
        import uvicorn

        from local_sql_manager.mcp_server import build_mcp, make_http_app

        mcp = build_mcp(library, read_only=read_only, token=token)
        app = make_http_app(mcp)
        # 配置端口被占用时改用系统分配的空闲端口：托盘复制出的配置会用
        # 实际端口，因此 agent 仍然连得上。
        listen_port = pick_port(port)
        config = uvicorn.Config(
            app,
            host=HOST,
            port=listen_port,
            log_level="warning",
            lifespan="on",
            access_log=False,
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(
            target=server.run, name="lsm-mcp", daemon=True
        )
        thread.start()
        # 启动失败时线程会提前退出，这里短暂等待以便把失败暴露成 None。
        deadline = time.monotonic() + 2
        while thread.is_alive() and not server.started:
            if time.monotonic() >= deadline:
                break
            thread.join(timeout=0.02)
        if not server.started:
            server.should_exit = True
            raise RuntimeError(f"MCP 服务未能启动（端口 {listen_port}）")
        actual = listen_port
        try:
            actual = server.servers[0].sockets[0].getsockname()[1]
        except (AttributeError, IndexError, OSError):
            logger.debug("无法读取 MCP 实际端口，沿用配置端口 %s", port)
        logger.info("MCP 服务已启动：http://%s:%s/mcp", HOST, actual)
        return McpHandle(server, thread, int(actual))
    except Exception:
        logger.exception("MCP 服务启动失败，界面继续运行")
        return None


def mcp_config_text(port=None, token=None) -> str | None:
    """返回可直接粘贴进 MCP 客户端配置的 JSON；端口无效时返回 None。

    传入 token 时输出 Authorization 头，MCP 客户端会原样带上。
    """
    if isinstance(port, bool):
        return None
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None
    if not 0 < port <= 65535:
        return None
    server = {"url": f"http://{HOST}:{port}/mcp"}
    if isinstance(token, str) and token.strip():
        server["headers"] = {"Authorization": f"Bearer {token.strip()}"}
    return json.dumps(
        {"mcpServers": {"local-sql-manager": server}},
        ensure_ascii=False,
        indent=2,
    )


def open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except webbrowser.Error:
        logger.exception("无法打开默认浏览器")


def _stop_component(component) -> None:
    if component is None:
        return
    for attribute in ("stop", "shutdown", "close"):
        action = getattr(component, attribute, None)
        if callable(action):
            try:
                action()
            except Exception:
                logger.exception("停止 %r 失败", component)
            return


def _show_error(message: str) -> None:
    """启动失败时弹原生对话框；无界面环境退回 stderr。"""
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(
                None, message, "本地多维表格管理器", 0x10
            )
            return
        except OSError:
            pass
    print(message, file=sys.stderr)


def main() -> int:
    if not acquire_single_instance(APP_MUTEX):
        port = load_config()["port"]
        _show_error(
            "本地多维表格管理器已在运行。\n"
            "请从系统托盘打开界面，或访问 "
            f"http://{HOST}:{port}/"
        )
        return 1

    library = None
    server = None
    web_thread = None
    mcp = None
    try:
        config = load_config()
        setup_logging()
        logger.info("启动 local-sql-manager，库目录 %s", config["libraries_dir"])

        path = library_path(config)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        library = open_library(path)

        app: Flask = create_app(library)
        port = pick_port(config["port"])
        server = make_server(HOST, port, app, threaded=True)
        web_thread = threading.Thread(
            target=server.serve_forever, name="lsm-web", daemon=True
        )
        web_thread.start()
        url = f"http://{HOST}:{port}/"
        logger.info("界面已启动：%s", url)

        mcp = start_mcp(
            library,
            port=int(config["mcp_port"]),
            read_only=bool(config["mcp_read_only"]),
            token=config["mcp_token"],
        )
        mcp_config = (
            partial(mcp_config_text, mcp.port, config["mcp_token"])
            if mcp is not None
            else None
        )
        open_browser(url)

        def shutdown() -> None:
            _stop_component(mcp)
            if server is not None:
                server.shutdown()

        try:
            run_tray(
                url=url,
                data_dir=data_dir(),
                log_dir=log_dir(),
                on_quit=shutdown,
                mcp_config=mcp_config,
            )
        except ImportError:
            logger.warning("未安装 pystray，改为前台运行，按 Ctrl+C 退出")
            try:
                web_thread.join()
            except KeyboardInterrupt:
                pass
    except Exception:
        logger.exception("启动失败")
        _show_error("启动本地多维表格管理器失败，详情见日志。")
        return 1
    finally:
        _stop_component(mcp)
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                logger.exception("停止 Web 服务失败")
        if web_thread is not None:
            web_thread.join(timeout=5)
        if library is not None:
            try:
                library.conn.close()
            except Exception:
                logger.exception("关闭库文件失败")
        release_single_instance(APP_MUTEX)
        logger.info("已退出 local-sql-manager")
    return 0


if __name__ == "__main__":
    sys.exit(main())
