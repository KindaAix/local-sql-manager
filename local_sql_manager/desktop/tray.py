"""系统托盘：打开界面、复制 MCP 配置、打开数据目录、打开日志、退出。

pystray 与 Pillow 只在真正创建托盘时导入，模块本身可在缺少可选依赖
的环境里被测试导入。
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

TRAY_MENU_LABELS = (
    "打开界面",
    "复制 MCP 配置",
    "打开数据目录",
    "打开日志",
    "退出",
)

ICON_BACKGROUND = (0x33, 0x70, 0xFF, 0xFF)


def build_icon_image(size: int = 64):
    """纯色应用图标，不依赖外部图片资源。"""
    from PIL import Image

    return Image.new("RGBA", (size, size), ICON_BACKGROUND)


def _set_clipboard_text(text: str) -> bool:
    """把文本写入 Windows 剪贴板；失败返回 False。"""
    if os.name != "nt":
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:
        return False

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]

    if not user32.OpenClipboard(None):
        return False
    try:
        if not user32.EmptyClipboard():
            return False
        buffer = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(buffer)
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            return False
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            kernel32.GlobalFree(handle)
            return False
        try:
            ctypes.memmove(pointer, buffer, size)
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            return False
        return True
    finally:
        user32.CloseClipboard()


def open_path(path: str | Path) -> None:
    """用系统默认程序打开目录或文件。"""
    target = str(path)
    try:
        if os.name == "nt":
            os.startfile(target)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
    except OSError:
        logger.exception("无法打开 %s", target)


def create_icon(
    *,
    url: str,
    data_dir: str | Path,
    log_dir: str | Path,
    on_quit,
    mcp_config=None,
):
    """构造 pystray 图标；调用方负责 run()。"""
    import pystray
    from pystray import MenuItem

    def open_ui(icon, item) -> None:
        import webbrowser

        webbrowser.open(url)

    def copy_mcp_config(icon, item) -> None:
        if mcp_config is None:
            return
        try:
            text = mcp_config()
        except Exception:
            logger.exception("生成 MCP 配置失败")
            return
        if not text:
            logger.warning("MCP 服务未启动，配置不可复制")
            return
        if not _set_clipboard_text(text):
            logger.warning("剪贴板不可用，MCP 配置未复制")

    def open_data(icon, item) -> None:
        open_path(data_dir)

    def open_logs(icon, item) -> None:
        open_path(log_dir)

    def quit_app(icon, item) -> None:
        try:
            on_quit()
        finally:
            icon.stop()

    menu = pystray.Menu(
        MenuItem(TRAY_MENU_LABELS[0], open_ui, default=True),
        MenuItem(TRAY_MENU_LABELS[1], copy_mcp_config, enabled=mcp_config is not None),
        MenuItem(TRAY_MENU_LABELS[2], open_data),
        MenuItem(TRAY_MENU_LABELS[3], open_logs),
        MenuItem(TRAY_MENU_LABELS[4], quit_app),
    )
    return pystray.Icon("local-sql-manager", build_icon_image(), "本地多维表格管理器", menu)


def run_tray(
    *,
    url: str,
    data_dir: str | Path,
    log_dir: str | Path,
    on_quit,
    mcp_config=None,
) -> None:
    """在当前线程运行托盘事件循环，直到选择「退出」。"""
    icon = create_icon(
        url=url,
        data_dir=data_dir,
        log_dir=log_dir,
        on_quit=on_quit,
        mcp_config=mcp_config,
    )
    icon.run()
