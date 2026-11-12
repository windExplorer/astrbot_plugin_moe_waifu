# -*- coding: utf-8 -*-
"""萌萌今日老婆 · Pillow 竖版立绘卡渲染（纯 Pillow，无 astrbot 依赖，可独立测试）。

版式 v3（v0.9.9，按用户反馈重做边框与布局）：
- 档位色宽边框：S 金 / A 紫 / B 蓝 / C 灰，14px 宽 + 纵向柔和渐变 +
  高斯泛光 + 内侧高光环，不同档位一眼能分辨；无档位回落角色应援色。
- 顶部标题胶囊与外框呼应：档位色柔光 + 档位色描边 + 深色玻璃底 +
  左侧档位色圆点（不再是突兀的灰胶囊 + 右挂小黄点）。
- 底部布局：名字左侧档位色竖条、独立细分隔线（不再紧贴名字像下划线）、
  作品眉标与名字拉开间距、各行间距统一、底部留白。
- 档位徽章（S 星 / A 菱 / B 环 / C 点）、字体、印象语自适应字号沿用 v2。
只有拿到立绘才会渲染（PRD 铁律 1：不存在无图版式）。
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

CARD_W, CARD_H = 700, 1200
_CARD_RADIUS = 30       # 圆角卡片
_FRAME_W = 14           # 档位色宽边框
_FRAME_GLOW = 16        # 边框泛光半径

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
    top_alpha, bottom_alpha = 0, 250
    ramp = Image.new("L", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        ramp.putpixel((0, y), int(top_alpha + (bottom_alpha - top_alpha) * (t ** 1.6)))
    alpha = ramp.resize((w, h))
    end = _shade(rgb, 0.24)          # 底部再压深一档：文字不压在人物轮廓上抢眼
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


def _frame_layers(w: int, h: int, rgb: tuple):
    """档位色宽边框：纵向柔和渐变 + 泛光 + 内侧高光环。

    Pillow 的 Draw 画不了渐变描边：先用 L 图做环带 mask，再用 1px 宽的纵向
    色带 resize 出渐变、把环带 mask 当 alpha；泛光由环带高斯模糊得到。
    三层都带 alpha，调用方必须逐层 Image.alpha_composite（Draw 直写不混合）。
    """
    ring = Image.new("L", (w, h), 0)
    ImageDraw.Draw(ring).rounded_rectangle(
        (0, 0, w - 1, h - 1), radius=_CARD_RADIUS, outline=255, width=_FRAME_W)
    top, bottom = _tint(rgb, 0.5), _shade(rgb, 0.6)
    ramp = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        ramp.putpixel((0, y), tuple(
            int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    frame = ramp.resize((w, h)).convert("RGBA")
    frame.putalpha(ring)

    glow = frame.filter(ImageFilter.GaussianBlur(_FRAME_GLOW))
    glow.putalpha(glow.getchannel("A").point(lambda a: int(a * 0.62)))

    inner = Image.new("L", (w, h), 0)
    ImageDraw.Draw(inner).rounded_rectangle(
        (_FRAME_W + 2, _FRAME_W + 2, w - 3 - _FRAME_W, h - 3 - _FRAME_W),
        radius=max(2, _CARD_RADIUS - _FRAME_W - 2), outline=255, width=2)
    hi = Image.new("RGBA", (w, h), (255, 255, 255, 0))
    hi.putalpha(inner.point(lambda a: int(a * 0.4)))
    return frame, glow, hi


def _pill_glow(size, box, rgb):
    """标题胶囊的档位色柔光（独立图层 + 高斯模糊，真混合）。"""
    g = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(g).rounded_rectangle(
        box, radius=(box[3] - box[1]) // 2, fill=(*rgb, 190))
    return g.filter(ImageFilter.GaussianBlur(13))


def _title_pill(draw, box, text, font, rgb) -> None:
    """标题胶囊：深色玻璃底 + 档位色描边 + 左侧档位色圆点 + 白字。"""
    x0, y0, x1, y1 = box
    draw.rounded_rectangle(box, radius=(y1 - y0) // 2,
                           fill=(14, 16, 24, 190), outline=(*rgb, 225), width=2)
    cy = (y0 + y1) // 2
    draw.ellipse((x0 + 14, cy - 7, x0 + 28, cy + 7), outline=(*rgb, 120), width=1)
    draw.ellipse((x0 + 17, cy - 4, x0 + 25, cy + 4), fill=(*rgb, 255))
    draw.text((x0 + 36, y0 + (y1 - y0 - font.size) // 2 - 1), text,
              font=font, fill=(255, 255, 255, 245))


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
    canvas.alpha_composite(_top_scrim(CARD_W, 210))
    overlay = _bottom_gradient(CARD_W, 680, rgb)
    canvas.alpha_composite(overlay, (0, CARD_H - 680))

    # ⚠️ 所有装饰画在独立透明层上，最后 alpha_composite 合成。Pillow 的 Draw
    # 无论默认还是 "RGBA" 模式都直写像素（ink 连 RGB 带 alpha 覆盖、不混合），
    # 所以半透明/泛光层必须先用 Image.alpha_composite 逐层叠好，再拿 Draw 画实体。
    body_s = load_font(22, data_dir=data_dir)
    body_xs = load_font(20, data_dir=data_dir)
    name_f = load_font(64, style="name", data_dir=data_dir)
    title_f = load_font(26, data_dir=data_dir)

    # 档位色边框（宽 + 柔和渐变 + 泛光 + 内高光）与标题胶囊柔光
    tt = "今日老婆"
    pill = (30, 30, 30 + int(title_f.getlength(tt)) + 62, 30 + title_f.size + 28)
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    frame, glow, hi = _frame_layers(CARD_W, CARD_H, t_rgb)
    for extra in (glow, frame, hi, _pill_glow(canvas.size, pill, t_rgb)):
        layer = Image.alpha_composite(layer, extra)
    draw = ImageDraw.Draw(layer)

    # 顶部左：档位色标题胶囊（呼应外框；♥ 字形多数 CJK 字体缺字，不用）
    _title_pill(draw, pill, tt, title_f, t_rgb)
    # 顶部右：档位徽章（不写字：S 星 / A 菱 / B 环 / C 点）
    _tier_emblem(draw, tier, t_rgb, CARD_W - 58, 58)

    # ---- 自底向上的内容布局（v0.9.9：间距统一、分隔线不再贴着名字）----
    bottom = CARD_H - 52
    # 最底行：日期（左）+ 重抽标（右，玻璃描边）
    draw.text((48, bottom - body_xs.size), f"{date_str}",
              font=body_xs, fill=(255, 255, 255, 175))
    if rerolled:
        rt = "重抽"
        tw = draw.textlength(rt, font=body_xs)
        bx = CARD_W - 48 - tw - 28
        draw.rounded_rectangle(
            (bx, bottom - body_xs.size - 7, CARD_W - 48, bottom + 9),
            radius=14, fill=(255, 255, 255, 40), outline=(255, 255, 255, 130), width=1,
        )
        draw.text((bx + 14, bottom - body_xs.size), rt,
                  font=body_xs, fill=(255, 255, 255, 235))
    bottom -= body_xs.size + 20

    # 统计行：档位色小圆点 + 文案
    stats = f"今日同款 {max(1, same_count)} 人 · 第 {max(1, total_count)} 次抽到她"
    dot_r = 4
    dot_y = bottom - body_s.size // 2 - 2
    draw.ellipse((48, dot_y - dot_r, 48 + dot_r * 2, dot_y + dot_r), fill=(*t_rgb, 255))
    _shadow_text(draw, (48 + dot_r * 2 + 12, bottom - body_s.size), stats, body_s, alpha=210)
    bottom -= body_s.size + 20

    # 印象语（字数自适应字号，最多 2~3 行）
    quote = str(entry.get("quote") or "").strip()
    if quote:
        qf, qlines = _quote_layout(quote, data_dir, CARD_W - 100)
        for line in reversed(qlines):
            _shadow_text(draw, (50, bottom - qf.size), line, qf, alpha=242)
            bottom -= qf.size + 10
        bottom -= 18

    # 独立细分隔线：与名字拉开距离，不再像给名字画下划线
    draw.rounded_rectangle((48, bottom - 3, 48 + 76, bottom),
                           radius=2, fill=(*t_rgb, 215))
    bottom -= 28

    # 名字（软润字体）+ 左侧档位色竖条
    name = str(entry.get("name") or "")
    draw.rounded_rectangle((46, bottom - name_f.size + 8, 52, bottom - 8),
                           radius=3, fill=(*t_rgb, 240))
    _shadow_text(draw, (62, bottom - name_f.size), name, name_f)
    bottom -= name_f.size + 16
    # 作品眉标：档位色小点 + 小字，与名字留足呼吸
    series = str(entry.get("series") or "").strip()
    if series:
        draw.ellipse((50, bottom - body_s.size + 8, 57, bottom - body_s.size + 15),
                     fill=(*t_rgb, 220))
        _shadow_text(draw, (66, bottom - body_s.size), series, body_s, alpha=205)

    # 圆角卡片：合成后统一裁圆角（泛光溢出圆角外的部分一并裁掉）
    mask = Image.new("L", (CARD_W, CARD_H), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, CARD_W - 1, CARD_H - 1),
                                           radius=_CARD_RADIUS, fill=255)
    out = Path(out_path) if out_path else src.with_name(f"card_{src.stem}.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.alpha_composite(layer)  # 装饰层真混合（半透明/阴影/胶囊都靠它）
    canvas.putalpha(mask)
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


# ---------- 帮助图（v0.17.0）：纯 Pillow 竖版说明卡，无外部依赖 ----------

_HELP_W = 700


def render_help(version: str = "", data_dir=None, out_path=None) -> str:
    """渲染帮助图，返回输出路径。纯 Pillow，不读立绘库（无图也可用）。"""
    W, pad = _HELP_W, 46
    # 内容驱动高度：指令 3 条 + 玩法 5 条 + 尾注
    H = 1092
    brand = (214, 51, 132)          # 品牌粉（与工坊一致）
    ink = (74, 52, 64)              # 正文墨色
    sub = (128, 108, 120)           # 次要文字
    line = (236, 214, 226)          # 分隔线

    if out_path is None:
        # 固定文件名覆盖写：帮助图内容随版本变化，旧图无需保留（防堆积）
        out_dir = Path(data_dir or ".") / "cache"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "help.png"

    # 底：竖向粉白渐变（浅粉到白），顶部品牌粉条压边
    img = Image.new("RGBA", (W, H), (255, 255, 255, 255))
    grad = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    for y in range(H):
        t = y / H
        gd.line((0, y, W, y), fill=(253, 240, 246, int(255 * (1 - t) * 0.55)))
    img.alpha_composite(grad)
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    ld.rectangle((0, 0, W, 10), fill=(*brand, 255))

    title_f = load_font(46, style="name", data_dir=data_dir)
    sub_f = load_font(21, style="soft", data_dir=data_dir)
    cmd_f = load_font(27, style="name", data_dir=data_dir)
    body_f = load_font(22, data_dir=data_dir)
    body_s = load_font(20, style="soft", data_dir=data_dir)
    head_f = load_font(24, style="name", data_dir=data_dir)

    def _dark_text(xy, text, font, fill=ink, alpha=255):
        ld.text((xy[0] + 1, xy[1] + 1), text, font=font, fill=(90, 60, 75, 36))
        ld.text(xy, text, font=font, fill=(*fill, alpha))

    y = 62
    _dark_text((pad, y), "萌萌今日老婆", title_f, ink)
    tw = title_f.getlength("萌萌今日老婆")
    _dark_text((pad + tw + 18, y + title_f.size - 24), version, sub_f, brand)
    y += title_f.size + 10
    _dark_text((pad, y), "每次抽到一位老婆，都值得一张专属立绘", sub_f, sub)
    y += sub_f.size + 30

    # 指令卡（白底圆角 + 粉描边）
    cmds = [
        ("/今日老婆", "抽取今天的角色，别名 /老婆 · /jrlp"),
        ("/换老婆", "重新抽一位，受「每日换老婆次数」限制"),
        ("/老婆帮助", "查看本帮助（别名 /waifu帮助）"),
    ]
    card_h = len(cmds) * 62 + 26
    ld.rounded_rectangle((pad - 8, y, W - pad + 8, y + card_h), radius=16,
                         fill=(255, 255, 255, 235), outline=(214, 51, 132, 90), width=1)
    cy = y + 16
    for cmd, desc in cmds:
        ld.rounded_rectangle((pad + 6, cy + 4, pad + 6 + cmd_f.getlength(cmd) + 26, cy + 4 + cmd_f.size + 14),
                             radius=12, fill=(214, 51, 132, 26))
        _dark_text((pad + 19, cy + 4), cmd, cmd_f, brand)
        _dark_text((pad + 30 + cmd_f.getlength(cmd) + 26, cy + 12), desc, body_s, sub)
        cy += 62
    y += card_h + 34

    # 玩法区
    _dark_text((pad, y), "玩法", head_f, ink)
    ld.rounded_rectangle((pad, y + head_f.size + 6, pad + 56, y + head_f.size + 10),
                         radius=2, fill=(*brand, 215))
    y += head_f.size + 26
    tips = [
        "一人一天一位：同一角色同一天人人同款，次日换新",
        "立绘库沉淀：抽到过的角色会积累立绘，满配额后秒出图",
        "卡图印象语与档位徽章随角色变化，换老婆解锁不同画法",
        "主题立绘：工坊可为角色按泳装 / 和风 / 职场等主题补立绘",
        "立绘不足时后台批量补图队列会自动生成（管理员配置）",
    ]
    for tip in tips:
        dot_y = y + body_s.size // 2
        ld.ellipse((pad + 2, dot_y - 4, pad + 10, dot_y + 4), fill=(*brand, 220))
        # 逐行折行（像素宽）
        lines = _wrap(tip, body_f, W - pad * 2 - 24, max_lines=2)
        for ln in lines:
            _dark_text((pad + 22, y), ln, body_f, ink, alpha=225)
            y += body_f.size + 8
        y += 12
    y += 8

    # 尾注：分隔线 + 管理提示
    ld.rounded_rectangle((pad, y, pad + 76, y + 3), radius=2, fill=(*brand, 150))
    y += 26
    _dark_text((pad, y), "角色库 / 试画 / 立绘维护 / 批量补图等管理功能", body_s, sub)
    y += body_s.size + 6
    _dark_text((pad, y), "在 AstrBot 后台的插件页（WebUI 工坊）里，仅管理员可见", body_s, sub)

    img.alpha_composite(layer)
    # 内容驱动高度：按实际内容底部裁掉多余空白（布局常数变化时自动适配）
    img = img.crop((0, 0, W, min(H, y + 56)))
    out = Image.new("RGB", img.size, (255, 255, 255))
    out.paste(img.convert("RGB"), (0, 0))
    out.save(out_path, "PNG")
    return str(out_path)
