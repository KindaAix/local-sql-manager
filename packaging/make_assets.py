"""生成安装包用的图标与闪屏。

输出（覆盖同名文件，可重复执行）：

- `packaging/app.ico`   多尺寸图标（16/32/48/64/128/256）
- `packaging/splash.png` Nuitka 启动闪屏（640x360）

风格：主色 #3370ff 的圆角色块 + 白色网格线条 + 应用名文字。字体优先用
系统自带微软雅黑；找不到字体时退化为无文字版本，保证脚本始终可用。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

PACKAGING_DIR = Path(__file__).resolve().parent

BRAND = (0x33, 0x70, 0xFF, 255)
WHITE = (255, 255, 255, 255)
INK = (0x1F, 0x23, 0x29, 255)
MUTED = (0x64, 0x6A, 0x73, 255)
SPLASH_BG = (0xF5, 0xF7, 0xFB, 255)

APP_NAME = "本地多维表格"
APP_TAGLINE = "正在启动，请稍候…"

ICON_SIZES = (16, 32, 48, 64, 128, 256)
ICON_MASTER = 1024
SPLASH_SIZE = (640, 360)

FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/msyhbd.ttc"),
    Path("C:/Windows/Fonts/msyh.ttc"),
    Path("C:/Windows/Fonts/simhei.ttf"),
)


def _load_font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | None:
    candidates = FONT_CANDIDATES if bold else FONT_CANDIDATES[1:] + FONT_CANDIDATES[:1]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    return None


def _draw_logo(size: int) -> Image.Image:
    """绘制 logo：圆角蓝色块 + 3x3 网格线条。"""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    margin = round(size * 0.06)
    radius = round(size * 0.19)
    draw.rounded_rectangle(
        (margin, margin, size - margin - 1, size - margin - 1),
        radius=radius,
        fill=BRAND,
    )

    left = round(size * 0.26)
    right = size - left
    top = round(size * 0.30)
    bottom = size - top
    line_width = max(2, round(size * 0.028))
    cell = (right - left) / 3

    for offset in (1, 2):
        x = round(left + cell * offset)
        y = round(top + (bottom - top) / 3 * offset)
        draw.line((x, top, x, bottom), fill=WHITE, width=line_width)
        draw.line((left, y, right, y), fill=WHITE, width=line_width)
    # 外框稍粗，视觉上收住网格
    frame = max(line_width, round(size * 0.036))
    draw.rectangle((left, top, right, bottom), outline=WHITE, width=frame)
    return image


def build_icon() -> Path:
    master = _draw_logo(ICON_MASTER)
    target = PACKAGING_DIR / "app.ico"
    icon = master.resize((256, 256), Image.LANCZOS)
    icon.save(target, format="ICO", sizes=[(s, s) for s in ICON_SIZES])
    return target


def build_splash() -> Path:
    width, height = SPLASH_SIZE
    image = Image.new("RGBA", SPLASH_SIZE, SPLASH_BG)
    draw = ImageDraw.Draw(image)

    logo_size = 112
    logo = _draw_logo(logo_size * 4).resize((logo_size, logo_size), Image.LANCZOS)
    logo_x = round(width * 0.12)
    logo_y = (height - logo_size) // 2
    image.alpha_composite(logo, (logo_x, logo_y))

    text_x = logo_x + logo_size + round(width * 0.06)
    title_font = _load_font(38, bold=True)
    tagline_font = _load_font(17)
    if title_font is not None:
        draw.text((text_x, height // 2 - 46), APP_NAME, font=title_font, fill=INK)
    if tagline_font is not None:
        draw.text(
            (text_x, height // 2 + 12),
            APP_TAGLINE,
            font=tagline_font,
            fill=MUTED,
        )

    target = PACKAGING_DIR / "splash.png"
    image.convert("RGB").save(target, format="PNG", optimize=True)
    return target


def main() -> int:
    icon = build_icon()
    splash = build_splash()
    print(f"已生成 {icon}")
    print(f"已生成 {splash}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
