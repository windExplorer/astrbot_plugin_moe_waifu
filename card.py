# -*- coding: utf-8 -*-
"""萌萌今日老婆 · Pillow 竖版立绘卡渲染（纯 Pillow，无 astrbot 依赖，可独立测试）。

版式 v2（v0.9.7，按用户反馈重做）：
- 档位可视化但不写字：S=金色五角星 / A=紫色菱形 / B=蓝色圆环 / C=灰圆点，
  右上角档位徽章 + 档位色内描边 + 档位色点缀（强调条/统计圆点）——
  不同档位的卡一眼能分辨；分类（动漫/游戏等）不再上卡。
- 字体：名字用等线 Bold（比楷体/雅黑软润），印象语用等线，统计/日期用雅黑；
  字体不打包、多级查找、找不到回落默认。
- 印象语按字数自适应字号（短句大字、长句小字），最多 3 行，放不下打省略号。
只有拿到立绘才会渲染（PRD 铁律 1：不存在无图版式）。
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CARD_W, CARD_H = 700, 1200
_CARD_RADIUS = 26  # 圆角卡片

# 常见 CJK 字体候选（正文/小字；Windows 在前，Linux/Docker Noto/文泉驿在后）
_BODY_FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
]
# 名字字体（软润优先：等线 Bold → 幼圆 → 雅黑粗 → 楷体）
_NAME_FONT_CANDIDATES = [
    "C:/Windows/Fonts/Dengb.ttf",
    "C:/Windows/Fonts/SIMYOU.TTF",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simkai.ttf",
    "C:/Windows/Fonts/STKAITI.TTF",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]
# 印象语字体（等线比雅黑柔）
_SOFT_FONT_CANDIDATES = [
    "C:/Windows/Fonts/Deng.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhl.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]

_FONT_CACHE: dict = {}

CATEGORY_LABELS = {"anime": "动漫", "gal": "galgame", "game": "游戏", "other": "其他"}

# 档位徽章配色（v0.9.7 档位可视化：取 tier 首字符；未列出的档位随角色色）
TIER_COLORS = {
    "S": (247, 201, 72),   # 金
    "A": (192, 132, 252),  # 紫
    "B": (96, 165, 250),   # 蓝
    "C": (148, 163, 184),  # 灰
}


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
                k in f.name.lower()
                for k in ("deng", "kai", "rounded", "youyuan", "wqy", "noto", "han")
            ):
                return str(f)
    return None


def load_font(size: int, style: str = "body", data_dir=None):
    """style：body=统计/日期（雅黑）· name=名字（等线 Bold）· soft=印象语（等线）。"""
    key = (size, style, str(data_dir))
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    cand = {
        "name": _NAME_FONT_CANDIDATES,
        "soft": _SOFT_FONT_CANDIDATES,
    }.get(style, _BODY_FONT_CANDIDATES)
    path = _find_font(cand, data_dir)
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


def _tint(rgb: tuple, factor: float) -> tuple:
    """向白色混合（factor 0~1），做高亮强调色用。"""
    return tuple(max(0, min(255, int(c + (255 - c) * factor))) for c in rgb)


def _luma(rgb: tuple) -> float:
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


def _tier_rgb(entry: dict, char_rgb: tuple) -> tuple:
    """档位强调色：S/A/B/C 有专属色，其它档位随角色色提亮一档。"""
    t = str(entry.get("tier") or "").strip()[:1].upper()
    return TIER_COLORS.get(t) or _tint(char_rgb, 0.3)


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
    top_alpha, bottom_alpha = 0, 246
    ramp = Image.new("L", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        ramp.putpixel((0, y), int(top_alpha + (bottom_alpha - top_alpha) * (t ** 1.6)))
    alpha = ramp.resize((w, h))
    end = _shade(rgb, 0.28)          # 底部深色化，保证白字对比
    layer = Image.new("RGB", (w, h), end)
    layer.putalpha(alpha)
    return layer


def _top_scrim(w: int, h: int) -> Image.Image:
    """顶部轻微压暗（黑，120→0 渐隐）：亮色立绘上标题仍可读。"""
    ramp = Image.new("L", (1, h))
    for y in range(h):
        t = 1.0 - y / max(1, h - 1)
        ramp.putpixel((0, y), int(120 * (t ** 1.4)))
    layer = Image.new("RGB", (w, h), (8, 10, 14))
    layer.putalpha(ramp.resize((w, h)))
    return layer


def _glass_pill(draw: ImageDraw.ImageDraw, x: int, y: int, text: str,
                font, pad_x=16, pad_y=7) -> int:
    """玻璃描边胶囊（半透明白底 + 白描边 + 白字），返回宽度（供下一枚定位）。"""
    tw = draw.textlength(text, font=font)
    h = int(font.size + pad_y * 2)
    draw.rounded_rectangle((x, y, x + tw + pad_x * 2, y + h), radius=max(2, h // 2),
                           fill=(255, 255, 255, 46), outline=(255, 255, 255, 130), width=1)
    draw.text((x + pad_x, y + pad_y - 1), text, font=font, fill=(255, 255, 255, 240))
    return int(tw + pad_x * 2)


def _tier_emblem(draw: ImageDraw.ImageDraw, tier: str, rgb: tuple,
                 cx: int, cy: int, r: int = 15) -> None:
    """档位徽章（v0.9.7，不写字）：S=五角星 A=菱形 B=圆环 C/其它=圆点。

    档位色实形 + 大一圈柔光底，右上角一眼分辨档位。
    """
    draw.ellipse((cx - r - 9, cy - r - 9, cx + r + 9, cy + r + 9), fill=(*rgb, 44))
    t = str(tier or "").strip()[:1].upper()
    if t == "S":
        pts = []
        for i in range(10):
            ang = -math.pi / 2 + i * math.pi / 5
            rr = r if i % 2 == 0 else r * 0.45
            pts.append((cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
        draw.polygon(pts, fill=(*rgb, 255))
        draw.ellipse((cx - 3, cy - 3, cx + 3, cy + 3), fill=(255, 255, 255, 200))
    elif t == "A":
        draw.polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)],
                     fill=(*rgb, 255))
        draw.polygon([(cx, cy - r * 0.42), (cx + r * 0.42, cy),
                      (cx, cy + r * 0.42), (cx - r * 0.42, cy)],
                     fill=(255, 255, 255, 110))
    elif t == "B":
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(*rgb, 255), width=4)
        draw.ellipse((cx - 3, cy - 3, cx + 3, cy + 3), fill=(*rgb, 255))
    else:
        rr = max(6, r * 0.6)
        draw.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=(*rgb, 235))


def _shadow_text(draw, xy, text, font, fill=(255, 255, 255), alpha=255) -> None:
    draw.text((xy[0] + 2, xy[1] + 2), text, font=font, fill=(0, 0, 0, 130))
    draw.text(xy, text, font=font, fill=(*fill, alpha) if len(fill) == 3 else fill)


def _wrap(text: str, font, max_w: int, max_lines: int = 2) -> list:
    """按像素宽度折行（中文逐字、ASCII 整词），最多 max_lines 行。"""
    lines, cur = [], ""
    for ch in str(text or ""):
        trial = cur + ch
        if font.getlength(trial) > max_w and cur:
            lines.append(cur)
            cur = ch
            if len(lines) == max_lines:
                break
        else:
            cur = trial
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines and len("".join(lines)) < len(str(text or "")):
        lines[max_lines - 1] = lines[max_lines - 1].rstrip() + "…"
    return lines


def _quote_layout(text: str, data_dir, max_w: int) -> tuple:
    """印象语按字数自适应字号（v0.9.7）：短句大字、长句小字多行（最多 3 行）。

    返回 (font, lines)；从大到小试，找到「能放得下」的第一档；都放不下按
    最小字号截断加省略号。
    """
    text = str(text or "").strip()
    n = len(text)
    bounds = ((30, 2, 16), (27, 2, 26), (24, 3, 42), (21, 3, 10 ** 9))
    chosen = None
    for size, max_lines, up_to in bounds:
        if n <= up_to or size == 21:
            chosen = (size, max_lines)
            break
    size, max_lines = chosen
    font = load_font(size, style="soft", data_dir=data_dir)
    return font, _wrap(text, font, max_w, max_lines=max_lines)


def render_card(portrait_path, entry: dict, *, date_str: str, same_count: int = 1,
                total_count: int = 1, rerolled: bool = False, out_path=None,
                data_dir=None) -> str:
    """渲染今日老婆卡，返回输出路径。portrait 必须存在（无图不渲染）。"""
    src = Path(portrait_path)
    if not src.is_file():
        raise FileNotFoundError(f"立绘不存在，拒绝渲染无图卡: {src}")
    rgb = _hex_rgb(entry.get("color"))
    tier = str(entry.get("tier") or "").strip()
    t_rgb = _tier_rgb(entry, rgb)
    canvas = _cover_fit(Image.open(src), CARD_W, CARD_H).convert("RGBA")

    # 顶部压暗（标题可读性）+ 底部渐变遮罩（角色色系）
    canvas.alpha_composite(_top_scrim(CARD_W, 190))
    overlay = _bottom_gradient(CARD_W, 620, rgb)
    canvas.alpha_composite(overlay, (0, CARD_H - 620))

    # ⚠️ 所有装饰画在独立透明层上，最后 alpha_composite 合成。不要直接在 RGBA
    # 画布上用 ImageDraw 画半透明——Pillow 的 Draw 无论默认还是 "RGBA" 模式都是
    # 直写像素（ink 连 RGB 带 alpha 一起覆盖、不混合），convert("RGB") 丢掉
    # alpha 后半透明白会全部变成实心白块（重抽标白字被吞掉的根因，已实测）。
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    body_s = load_font(22, data_dir=data_dir)
    body_xs = load_font(20, data_dir=data_dir)
    name_f = load_font(64, style="name", data_dir=data_dir)

    # ---- 自底向上的内容布局 ----
    bottom = CARD_H - 36
    # 最底行：日期（左）+ 重抽标（右，玻璃描边）
    draw.text((46, bottom - body_xs.size), f"{date_str}",
              font=body_xs, fill=(255, 255, 255, 170))
    if rerolled:
        rt = "重抽"
        tw = draw.textlength(rt, font=body_xs)
        bx = CARD_W - 46 - tw - 28
        draw.rounded_rectangle(
            (bx, bottom - body_xs.size - 7, CARD_W - 46, bottom + 9),
            radius=14, fill=(255, 255, 255, 40), outline=(255, 255, 255, 130), width=1,
        )
        draw.text((bx + 14, bottom - body_xs.size), rt,
                  font=body_xs, fill=(255, 255, 255, 235))
    bottom -= body_xs.size + 18

    # 统计行：档位色小圆点 + 文案
    stats = f"今日同款 {max(1, same_count)} 人 · 第 {max(1, total_count)} 次抽到她"
    dot_r = 4
    dot_y = bottom - body_s.size // 2 - 2
    draw.ellipse((46, dot_y - dot_r, 46 + dot_r * 2, dot_y + dot_r), fill=(*t_rgb, 255))
    _shadow_text(draw, (46 + dot_r * 2 + 12, bottom - body_s.size), stats, body_s, alpha=205)
    bottom -= body_s.size + 18

    # 印象语（字数自适应字号，最多 2~3 行）
    quote = str(entry.get("quote") or "").strip()
    if quote:
        qf, qlines = _quote_layout(quote, data_dir, CARD_W - 96)
        for line in reversed(qlines):
            _shadow_text(draw, (48, bottom - qf.size), line, qf, alpha=242)
            bottom -= qf.size + 10
        bottom -= 14

    # 档位色强调条（名字与印象语之间的分隔点缀）
    draw.rounded_rectangle((46, bottom - 5, 46 + 64, bottom), radius=3, fill=(*t_rgb, 235))
    bottom -= 22

    # 名字（软润字体）+ 作品（小字，位于名字上方）
    name = str(entry.get("name") or "")
    _shadow_text(draw, (44, bottom - name_f.size), name, name_f)
    bottom -= name_f.size + 4
    series = str(entry.get("series") or "").strip()
    if series:
        _shadow_text(draw, (48, bottom - body_s.size), series, body_s, alpha=200)

    # 顶部左：标题玻璃胶囊（♥ 字形多数 CJK 字体缺字，会渲染成方块，不用）
    title_f = load_font(26, data_dir=data_dir)
    tt = "今日老婆"
    tw = draw.textlength(tt, font=title_f)
    draw.rounded_rectangle((30, 30, 30 + tw + 44, 30 + title_f.size + 24),
                           radius=18, fill=(10, 12, 18, 150),
                           outline=(255, 255, 255, 90), width=1)
    draw.text((52, 42), tt, font=title_f, fill=(255, 255, 255, 240))
    draw.ellipse((30 + tw + 44 - 20, 30 + title_f.size + 8,
                  30 + tw + 44 - 10, 30 + title_f.size + 18), fill=(*t_rgb, 255))

    # 顶部右：档位徽章（不写字：S 星 / A 菱 / B 环 / C 点，档位色）
    _tier_emblem(draw, tier, t_rgb, CARD_W - 52, 54)

    # 内描边（档位有专属色时随档位色，否则细白线）
    border_alpha = 130 if tier else 80
    draw.rounded_rectangle((1, 1, CARD_W - 2, CARD_H - 2), radius=_CARD_RADIUS - 1,
                           outline=(*t_rgb, border_alpha) if tier
                           else (255, 255, 255, border_alpha), width=2)

    # 圆角卡片：圆角外像素透明
    mask = Image.new("L", (CARD_W, CARD_H), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, CARD_W - 1, CARD_H - 1),
                                           radius=_CARD_RADIUS, fill=255)
    canvas.putalpha(mask)

    out = Path(out_path) if out_path else src.with_name(f"card_{src.stem}.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.alpha_composite(layer)  # 装饰层真混合（半透明/阴影/胶囊都靠它）
    canvas.save(out, "PNG", optimize=True)
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
