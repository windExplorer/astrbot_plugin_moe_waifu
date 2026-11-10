# -*- coding: utf-8 -*-
"""萌萌今日老婆 · Pillow 竖版立绘卡渲染（纯 Pillow，无 astrbot 依赖，可独立测试）。

版式（700×1200）：立绘全幅打底 → 底部「角色应援色」渐变遮罩 → 白字信息面板。
整卡随 entry.color 定色（星语「整卡随签定色」同款思路）；徽章白底圆角 + 深色文字；
展示文字（名字）用楷体系，正文用基础字体；字体不打包、多级查找、找不到回落默认。
只有拿到立绘才会渲染（PRD 铁律 1：不存在无图版式）。
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CARD_W, CARD_H = 700, 1200

# 常见 CJK 字体候选（正文；Windows 在前，Linux/Docker Noto/文泉驿在后）
_BODY_FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
]
# 展示字体候选（楷体系——「老婆名」的字面感）
_DISPLAY_FONT_CANDIDATES = [
    "C:/Windows/Fonts/simkai.ttf",
    "C:/Windows/Fonts/STKAITI.TTF",
    "C:/Windows/Fonts/KAIU.TTF",
]

_FONT_CACHE: dict = {}

CATEGORY_LABELS = {"anime": "动漫", "gal": "galgame", "game": "游戏", "other": "其他"}


def _font_dirs(data_dir=None) -> list:
    """额外字体目录：插件数据目录 fonts/ → AstrBot 公共 data/fonts/（moe 系同款约定）。"""
    dirs = []
    if data_dir is not None:
        dirs.append(Path(data_dir) / "fonts")
    dirs.append(Path("data/fonts"))
    return [d for d in dirs if d.is_dir()]


def _find_font(candidates: list, data_dir=None) -> str | None:
    """系统候选优先；再扫插件/AstrBot 字体目录里名字含 hint 的文件。"""
    for cand in candidates:
        if Path(cand).is_file():
            return cand
    for d in _font_dirs(data_dir):
        for f in d.iterdir():
            if f.suffix.lower() in (".ttf", ".otf", ".ttc") and any(
                k in f.name.lower() for k in ("kai", "rounded", "wqy", "noto", "han")
            ):
                return str(f)
    return None


def load_font(size: int, display: bool = False, data_dir=None):
    key = (size, display, str(data_dir))
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    path = _find_font(_DISPLAY_FONT_CANDIDATES if display else _BODY_FONT_CANDIDATES, data_dir)
    try:
        font = ImageFont.truetype(path, size) if path else ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


def _hex_rgb(color: str, fallback=(232, 180, 200)) -> tuple:
    m = str(color or "").strip().lstrip("#")
    if len(m) == 6:
        try:
            return tuple(int(m[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            pass
    return fallback


def _shade(rgb: tuple, factor: float) -> tuple:
    return tuple(max(0, min(255, int(c * factor))) for c in rgb)


def _cover_fit(img: Image.Image, w: int, h: int) -> Image.Image:
    """等比缩放铺满画布（居中裁切）。立绘默认就是 700×1200，一般不裁。"""
    if img.width == w and img.height == h:
        return img.convert("RGB")
    scale = max(w / img.width, h / img.height)
    nw, nh = max(w, round(img.width * scale)), max(h, round(img.height * scale))
    img = img.convert("RGB").resize((nw, nh), Image.LANCZOS)
    x = (nw - w) // 2
    y = (nh - h) // 2
    return img.crop((x, y, x + w, y + h))


def _bottom_gradient(w: int, h: int, rgb: tuple) -> Image.Image:
    """自下而上的遮罩：底部≈角色色加深不透明，向上渐隐。"""
    top_alpha, bottom_alpha = 0, 235
    ramp = Image.new("L", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        ramp.putpixel((0, y), int(top_alpha + (bottom_alpha - top_alpha) * (t ** 1.5)))
    alpha = ramp.resize((w, h))
    end = _shade(rgb, 0.32)          # 底部深色化，保证白字对比
    layer = Image.new("RGB", (w, h), end)
    layer.putalpha(alpha)
    return layer


def _pill(draw: ImageDraw.ImageDraw, x: int, y: int, text: str, fg: tuple,
          font, pad_x=16, pad_y=7) -> int:
    """白色圆角胶囊徽章，返回宽度（供下一枚定位）。"""
    tw = draw.textlength(text, font=font)
    h = int(font.size + pad_y * 2)
    draw.rounded_rectangle((x, y, x + tw + pad_x * 2, y + h), radius=max(2, h // 2),
                           fill=(255, 255, 255, 235))
    draw.text((x + pad_x, y + pad_y - 1), text, font=font, fill=fg)
    return int(tw + pad_x * 2)


def _shadow_text(draw, xy, text, font, fill=(255, 255, 255), alpha=255) -> None:
    draw.text((xy[0] + 2, xy[1] + 2), text, font=font, fill=(0, 0, 0, 110))
    draw.text(xy, text, font=font, fill=(*fill, alpha) if len(fill) == 3 else fill)


def _wrap(text: str, font, max_w: int) -> list:
    """按像素宽度折行（中文逐字、ASCII 整词），最多 2 行。"""
    lines, cur = [], ""
    for ch in str(text or ""):
        trial = cur + ch
        if font.getlength(trial) > max_w and cur:
            lines.append(cur)
            cur = ch
            if len(lines) == 2:
                break
        else:
            cur = trial
    if cur and len(lines) < 2:
        lines.append(cur)
    if len(lines) == 2 and len("".join(lines)) < len(str(text or "")):
        lines[1] = lines[1].rstrip() + "…"
    return lines


def render_card(portrait_path, entry: dict, *, date_str: str, same_count: int = 1,
                total_count: int = 1, rerolled: bool = False, out_path=None,
                data_dir=None) -> str:
    """渲染今日老婆卡，返回输出路径。portrait 必须存在（无图不渲染）。"""
    src = Path(portrait_path)
    if not src.is_file():
        raise FileNotFoundError(f"立绘不存在，拒绝渲染无图卡: {src}")
    rgb = _hex_rgb(entry.get("color"))
    canvas = _cover_fit(Image.open(src), CARD_W, CARD_H).convert("RGBA")

    # 底部渐变遮罩（角色色系）
    overlay = _bottom_gradient(CARD_W, 560, rgb)
    canvas.alpha_composite(overlay, (0, CARD_H - 560))

    # ⚠️ 所有装饰画在独立透明层上，最后 alpha_composite 合成。不要直接在 RGBA
    # 画布上用 ImageDraw 画半透明——Pillow 的 Draw 无论默认还是 "RGBA" 模式都是
    # 直写像素（ink 连 RGB 带 alpha 一起覆盖、不混合），convert("RGB") 丢掉
    # alpha 后半透明白会全部变成实心白块（重抽标白字被吞掉的根因，已实测）。
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    body_l = load_font(27, data_dir=data_dir)
    body_s = load_font(23, data_dir=data_dir)
    body_xs = load_font(21, data_dir=data_dir)
    name_f = load_font(62, display=True, data_dir=data_dir)
    quote_f = load_font(27, data_dir=data_dir)

    fg = entry.get("color") or "#E8B4C8"
    fg_rgb = _shade(_hex_rgb(fg), 0.55)  # 白底徽章上的深色文字

    # ---- 自底向上的内容布局 ----
    bottom = CARD_H - 34
    # 最底行：日期（左）+ 重抽标（右）
    date_text = f"{date_str}"
    draw.text((46, bottom - body_xs.size), date_text, font=body_xs, fill=(255, 255, 255, 185))
    if rerolled:
        rt = "重抽"
        tw = draw.textlength(rt, font=body_xs)
        draw.rounded_rectangle(
            (CARD_W - 46 - tw - 24, bottom - body_xs.size - 6,
             CARD_W - 46, bottom + 8),
            radius=13, fill=(255, 255, 255, 46),
        )
        draw.text((CARD_W - 46 - tw - 12, bottom - body_xs.size), rt,
                  font=body_xs, fill=(255, 255, 255, 225))
    bottom -= body_xs.size + 22

    # 统计行
    stats = f"今日同款 {max(1, same_count)} 人 · 第 {max(1, total_count)} 次抽到她"
    _shadow_text(draw, (46, bottom - body_s.size), stats, body_s, alpha=235)
    bottom -= body_s.size + 20

    # 细分隔线
    draw.line((46, bottom, CARD_W - 46, bottom), fill=(255, 255, 255, 90), width=1)
    bottom -= 20

    # 印象语（最多两行）
    quote = str(entry.get("quote") or "").strip()
    if quote:
        for line in reversed(_wrap(quote, quote_f, CARD_W - 92)):
            _shadow_text(draw, (46, bottom - quote_f.size), line, quote_f, alpha=240)
            bottom -= quote_f.size + 12
        bottom -= 8

    # 名字 + 作品
    name = str(entry.get("name") or "")
    _shadow_text(draw, (44, bottom - name_f.size), name, name_f)
    bottom -= name_f.size + 10
    series = str(entry.get("series") or "").strip()
    if series:
        _shadow_text(draw, (48, bottom - body_l.size), series, body_l, alpha=220)
        bottom -= body_l.size + 14

    # 徽章行（分类 + tier）
    bx, by = 46, bottom - 34
    cat = CATEGORY_LABELS.get(entry.get("category"), "其他")
    x = bx
    x += _pill(draw, x, by, cat, fg_rgb, body_s) + 10
    tier = str(entry.get("tier") or "").strip()
    if tier:
        x += _pill(draw, x, by, tier, fg_rgb, body_s)

    # 顶部左：标题小胶囊（♥ 字形多数 CJK 字体缺字，会渲染成方块，不用）
    title_f = load_font(26, data_dir=data_dir)
    tt = "今日老婆"
    tw = draw.textlength(tt, font=title_f)
    draw.rounded_rectangle((30, 30, 30 + tw + 40, 30 + title_f.size + 22),
                           radius=17, fill=(255, 255, 255, 200))
    draw.text((50, 40), tt, font=title_f, fill=fg_rgb)

    out = Path(out_path) if out_path else src.with_name(f"card_{src.stem}.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.alpha_composite(layer)  # 装饰层真混合（半透明/阴影/胶囊都靠它）
    canvas.convert("RGB").save(out, "PNG", optimize=True)
    return str(out)


def sample_card_demo(portrait_path, out_path) -> str:
    """测试/预览用：用占位立绘渲染一张示例卡（初音未来配色），返回输出路径。"""
    entry = {
        "name": "初音未来", "series": "VOCALOID", "category": "anime",
        "tier": "S", "color": "#39C5BB",
        "quote": "初次见面，我是初音未来！从今天起，就是你的专属歌姬了哦。",
    }
    if portrait_path is None:
        img = Image.new("RGB", (CARD_W, CARD_H))
        d = ImageDraw.Draw(img)
        for y in range(CARD_H):
            t = y / CARD_H
            d.line((0, y, CARD_W, y),
                   fill=(int(60 + 80 * t), int(190 - 40 * t), int(200 + 30 * t)))
        d.text((CARD_W // 2 - 60, CARD_H // 2 - 20), "SAMPLE", fill=(255, 255, 255))
        tmp = Path(out_path).with_name("sample_portrait.png")
        img.save(tmp)
        portrait_path = tmp
    return render_card(portrait_path, entry, date_str="2026-11-11",
                       same_count=3, total_count=12, rerolled=False,
                       out_path=out_path)
