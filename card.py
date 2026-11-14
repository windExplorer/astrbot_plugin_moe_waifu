# -*- coding: utf-8 -*-
"""萌萌今日老婆 · Pillow 竖版立绘卡渲染（纯 Pillow，无 astrbot 依赖，可独立测试）。

版式 v4（v0.45.0，按用户反馈重做：去金边 / 档位印章 / 排版层次）：
- **去掉宽边框**（用户：金边没啥必要，也可能只是现在的金边太丑）：立绘满版铺到
  圆角边上，最外圈只留 1px 半透明细线收边。档位色从此只做**小面积点缀**
  （印章 / 眉标竖线 / 印象语引文竖线 / 统计圆点），不再刷满一圈。
- **档位改成印章**（用户：别再用五角星）：圆角方块 + 档位色纵向渐变 + 白色内
  描边 + 漫画体大写字母，整体微倾 -7°（贴纸感），S 金 / A 紫 / B 蓝 / C 灰仍
  一眼分辨，但只在名字右侧出现一次。
- **排版分层**（用户：现在没有层次感）。自上而下字重与透明度逐级递减：
  作品眉标（小字 + 档位色竖线）→ 角色名（漫画体大标题）→ 一句话简介
  （profile.tagline → bio → title，柔和小字）→ 档位色分隔短线 →
  印象语（左侧引文竖线）→ 统计行（档位色圆点）→ 日期（最淡）。
- **去掉左上角「今日老婆」胶囊**（用户：好像不要会更好）。
- **漫画体**（用户：字体不太好看，能用漫画体吗）：随包 `fonts/` 分发一款开源
  漫画体（站酷快乐体，SIL OFL 1.1），名字 / 档位字母 / 印象语优先用它；用户也
  可把自己的字体丢进 `data_dir/fonts/`（文件名含 comic/kuaile/漫画 等即优先）。
  漫画体缺字（生僻日文汉字，如「凪」「雫」）逐字回落等线，绝不出现豆腐块。
只有拿到立绘才会渲染（PRD 铁律 1：不存在无图版式）。
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

CARD_W, CARD_H = 700, 1200
_CARD_RADIUS = 30       # 圆角卡片
_PAD_X = 54             # 左右安全边（文字一律从这里起排）
_SEAL = 92              # 档位印章边长（不含内边距）

_FONT_DIR = Path(__file__).resolve().parent / "fonts"   # 随包字体（漫画体）

# 常见 CJK 字体候选（正文/小字；Windows 在前，Linux/Docker Noto/文泉驿在后）
_BODY_FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
]
# 名字回退字体（漫画体缺字时逐字回落；软润优先：等线 Bold → 幼圆 → 雅黑粗 → 楷体）
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
# 漫画体系统回退（大多数机器没装，命中算赚；随包字体找不到时才用）
_COMIC_FONT_CANDIDATES = [
    "C:/Windows/Fonts/STHUPO.TTF",      # 华文琥珀
    "C:/Windows/Fonts/SIMYOU.TTF",      # 幼圆
    "C:/Windows/Fonts/STXINGKA.TTF",    # 华文行楷
    "C:/Windows/Fonts/msyhbd.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
]
# 用户自带字体（data_dir/fonts/）里命中这些关键字即当漫画体
_COMIC_HINTS = ("comic", "kuaile", "manga", "smiley", "zcool", "cute",
                "rounded", "youyuan", "hupo", "快乐", "漫画", "圆体", "漫黑")
_SOFT_HINTS = ("deng", "kai", "rounded", "youyuan", "wqy", "noto", "han")

_FONT_CACHE: dict = {}
_COMIC_PATH_CACHE: dict = {}

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


def _font_files(d) -> list:
    """目录里的字体文件（排序保证确定性，避免不同机器挑到不同文件）。"""
    try:
        return sorted(p for p in Path(d).iterdir()
                      if p.is_file() and p.suffix.lower() in (".ttf", ".otf", ".ttc"))
    except Exception:
        return []


def _pick_by_hint(dirs, hints) -> str | None:
    for d in dirs:
        for f in _font_files(d):
            if any(k in f.name.lower() for k in hints):
                return str(f)
    return None


def _comic_font_path(data_dir=None) -> str | None:
    """漫画体查找顺序：用户字体目录 → 随包 fonts/ → 系统候选；都没有返回 None。

    用户把自己的漫画体丢进 `data/plugin_data/<插件>/fonts/`（文件名带 comic /
    kuaile / 漫画 等关键字）就能覆盖随包字体——不必改代码、不必重装插件。
    """
    key = str(data_dir)
    if key in _COMIC_PATH_CACHE:
        return _COMIC_PATH_CACHE[key]
    path = _pick_by_hint(_font_dirs(data_dir), _COMIC_HINTS)
    if not path:
        files = _font_files(_FONT_DIR)
        path = next((str(f) for f in files
                     if any(k in f.name.lower() for k in _COMIC_HINTS)), None)
        if not path and files:
            path = str(files[0])       # 随包目录只放一款，没命中关键字也照用
    if not path:
        path = next((c for c in _COMIC_FONT_CANDIDATES if Path(c).is_file()), None)
    _COMIC_PATH_CACHE[key] = path
    return path


def _find_font(candidates: list, data_dir=None, hints=_SOFT_HINTS) -> str | None:
    """系统候选优先；再扫插件/AstrBot 字体目录里名字含 hint 的文件。"""
    for cand in candidates:
        if Path(cand).is_file():
            return cand
    return _pick_by_hint(_font_dirs(data_dir), hints)


def load_font(size: int, style: str = "body", data_dir=None):
    """style：body=正文（雅黑）· name=名字回退（等线 Bold）· soft=小字（等线）
    · comic=漫画体（随包站酷快乐体，回落后再走 name/body）。"""
    key = (size, style, str(data_dir))
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    if style == "comic":
        path = _comic_font_path(data_dir)
    else:
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


# ---------- 逐字字体回落（漫画体缺字 → 等线） ----------
# 站酷快乐体覆盖 GB2312 全字表，但生僻日文汉字（凪 / 雫 / 栞 / 緒 …）没有字形。
# 缺字检测：把该字渲染出的位图与「必然缺字」的 PUA 位图比对，相同即缺字。
_GLYPH_CACHE: dict = {}
_NOTDEF_CACHE: dict = {}


def _mask_sig(font, ch):
    """把一个字渲染成小图并取字节签名（ImagingCore 没有 tobytes，只能走 Image）。"""
    try:
        size = max(8, int(getattr(font, "size", 20)))
        img = Image.new("L", (size * 2, size * 2), 0)
        ImageDraw.Draw(img).text((2, 2), ch, font=font, fill=255)
    except Exception:
        return None
    return img.tobytes()


def _has_glyph(font, ch) -> bool:
    if not ch or ch.isspace():
        return True
    key = (id(font), ch)
    hit = _GLYPH_CACHE.get(key)
    if hit is not None:
        return hit
    sig = _mask_sig(font, ch)
    nd = _NOTDEF_CACHE.get(id(font))
    if nd is None:
        nd = _mask_sig(font, "\ue05a")
        _NOTDEF_CACHE[id(font)] = nd
    ok = True if (sig is None or nd is None) else (sig != nd)
    _GLYPH_CACHE[key] = ok
    return ok


def _runs(text: str, primary, fallback=None) -> list:
    """按「该字在主字体里有没有字形」切成若干 (字体, 文本块) 连续段。"""
    out: list = []
    for ch in str(text or ""):
        f = primary if (fallback is None or _has_glyph(primary, ch)) else fallback
        if out and out[-1][0] is f:
            out[-1][1].append(ch)
        else:
            out.append((f, [ch]))
    return [(f, "".join(cs)) for f, cs in out]


def text_width(text: str, font, fallback=None) -> float:
    return sum(f.getlength(chunk) for f, chunk in _runs(text, font, fallback))


def _mixed(layer, xy, text, font, fallback=None, fill=(255, 255, 255, 255),
           shadow=None, spacing: float = 0.0):
    """混合字体绘制（半透明层内），返回文本结束的 x 坐标。

    spacing>0 时逐字加字距（中文小标题拉开一点更透气）。
    """
    x, y = xy
    runs = _runs(text, font, fallback)
    if shadow:
        sx = x
        for f, chunk in runs:
            ImageDraw.Draw(layer).text((sx + 2, y + 2), chunk, font=f, fill=shadow)
            sx += f.getlength(chunk) + (spacing * len(chunk) if spacing else 0)
    d = ImageDraw.Draw(layer)
    for f, chunk in runs:
        if spacing:
            for ch in chunk:
                d.text((x, y), ch, font=f, fill=fill)
                x += f.getlength(ch) + spacing
        else:
            d.text((x, y), chunk, font=f, fill=fill)
            x += f.getlength(chunk)
    return x


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


def _pw(t: float, knots) -> float:
    """折线插值 + smoothstep 平滑（knots 为 (t, value) 升序）。"""
    for (t0, v0), (t1, v1) in zip(knots, knots[1:]):
        if t <= t1:
            if t1 <= t0:
                return v1
            f = (t - t0) / (t1 - t0)
            return v0 + (v1 - v0) * (f * f * (3 - 2 * f))
    return knots[-1][1]


def _ramp(h: int, knots, peak: float, w: int = 1) -> Image.Image:
    r = Image.new("L", (1, h))
    for y in range(h):
        r.putpixel((0, y), int(peak * _pw(y / max(1, h - 1), knots)))
    return r.resize((w, h))


def _bottom_scrim(w: int, h: int, rgb: tuple) -> Image.Image:
    """底部双层遮罩：黑色压暗（保文字对比）+ 角色色染色（保氛围）。

    不做满版边框，层次全交给这层渐变：立绘上半身完整露出，下半身文字浮在
    由「黑 → 角色色深调」过渡出来的暗场里。

    曲线用折线 + smoothstep 而不是幂函数：亮色立绘（白 / 浅粉）上白字要能读，
    必须让暗场在文字区（下半部 40%）迅速到位；幂函数曲线在那里永远偏浅。
    """
    # 文字区在卡高 55%~100%：那里给到 0.75+ 的压暗，白字才够对比度；
    # 上沿斜坡拉长（0.12→0.62），避免出现一条「硬边横带」把立绘切断。
    dark_knots = ((0.0, 0.0), (0.12, 0.03), (0.30, 0.26), (0.46, 0.58),
                  (0.62, 0.82), (0.82, 0.93), (1.0, 1.0))
    tint_knots = ((0.0, 0.0), (0.32, 0.14), (0.62, 0.52), (1.0, 1.0))
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    black = Image.new("RGB", (w, h), (6, 8, 12))
    black.putalpha(_ramp(h, dark_knots, 246, w))
    out = Image.alpha_composite(out, black)
    tint = Image.new("RGB", (w, h), _shade(rgb, 0.46))
    tint.putalpha(_ramp(h, tint_knots, 150, w))
    return Image.alpha_composite(out, tint)


def _tier_seal(size: int, letter: str, rgb: tuple, data_dir=None) -> Image.Image:
    """档位印章（v0.45.0，替代五角星徽章）：圆角方块 + 档位色纵向渐变 +
    白色内描边 + 漫画体大写字母，整体微倾 -7°（贴纸感）。

    返回带 16px 内边距的 RGBA 小图；调用方按「视觉块」定位即可
    （视觉块 = 返回图向内缩 16px）。
    """
    pad = 16
    s = size + pad * 2
    layer = Image.new("RGBA", (s, s), (0, 0, 0, 0))

    # 渐变底：圆角掩膜裁形，再用 1px 宽的纵向渐变上色（Draw 画不了渐变描边）
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=int(size * 0.24), fill=255)
    top, bot = _tint(rgb, 0.36), _shade(rgb, 0.72)
    ramp = Image.new("RGBA", (1, size))
    for y in range(size):
        t = y / max(1, size - 1)
        ramp.putpixel((0, y), tuple(int(top[i] + (bot[i] - top[i]) * t)
                                    for i in range(3)) + (236,))
    fill = ramp.resize((size, size))
    fill.putalpha(mask.point(lambda a: a * 236 // 255))
    layer.alpha_composite(fill, (pad, pad))

    d = ImageDraw.Draw(layer)
    d.rounded_rectangle((pad + 6, pad + 6, pad + size - 7, pad + size - 7),
                        radius=int(size * 0.17), outline=(255, 255, 255, 96), width=2)

    # 字母用「名字字体」（等线 Bold / Noto Sans CJK Bold）而不是漫画体：漫画体的
    # A~D 过于花哨（B 像折角、C 像方括号），印章上一眼认错档位得不偿失。
    text = (str(letter or "").strip()[:1] or "·").upper()
    lf = load_font(int(size * 0.56), style="name", data_dir=data_dir)
    fb = load_font(int(size * 0.56), style="comic", data_dir=data_dir)
    cx, cy = pad + size / 2, pad + size / 2
    bb = lf.getbbox(text)
    x = cx - text_width(text, lf, fb) / 2
    ty = cy - (bb[3] - bb[1]) / 2 - bb[1]
    for f, chunk in _runs(text, lf, fb):
        d.text((x, ty), chunk, font=f, fill=(255, 255, 255, 248))
        x += f.getlength(chunk)
    return layer.rotate(-7, resample=Image.BICUBIC, center=(s / 2, s / 2))


def _wrap(text: str, font, max_w: int, max_lines: int = 2, fallback=None) -> list:
    """按像素宽度折行（中文逐字），最多 max_lines 行；fallback 支持缺字回落。"""
    lines, cur, cur_w = [], "", 0.0
    src = str(text or "")
    for ch in src:
        f = font if (fallback is None or _has_glyph(font, ch)) else fallback
        w = f.getlength(ch)
        if cur and cur_w + w > max_w:
            lines.append(cur)
            cur, cur_w = ch, w
            if len(lines) == max_lines:
                break
        else:
            cur, cur_w = cur + ch, cur_w + w
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if lines and len(lines) == max_lines and len("".join(lines)) < len(src):
        lines[max_lines - 1] = lines[max_lines - 1].rstrip() + "…"
    return lines


def _quote_layout(text: str, data_dir, max_w: int) -> tuple:
    """印象语按字数自适应字号：短句大字、长句小字多行（最多 3 行）。

    返回 (漫画体 font, 回落 font, lines)；从大到小试，找到「能放得下」的第一
    档；都放不下按最小字号截断加省略号。
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
    font = load_font(size, style="comic", data_dir=data_dir)
    fb = load_font(size, style="soft", data_dir=data_dir)
    return font, fb, _wrap(text, font, max_w, max_lines=max_lines, fallback=fb)


def render_card(portrait_path, entry: dict, *, date_str: str, same_count: int = 1,
                total_count: int = 1, rerolled: bool = False, out_path=None,
                data_dir=None) -> str:
    """渲染今日老婆卡，返回输出路径。portrait 必须存在（无图不渲染）。"""
    src = Path(portrait_path)
    if not src.is_file():
        raise FileNotFoundError(f"立绘不存在，拒绝渲染无图卡: {src}")
    entry = entry or {}
    rgb = _hex_rgb(entry.get("color"))
    t_rgb = _tier_rgb(entry, rgb)
    canvas = _cover_fit(Image.open(src), CARD_W, CARD_H).convert("RGBA")

    # 底部双层渐变（v0.45.0：不画宽边框，层次交给渐变 + 排版）。
    # 高度 860 是实测定下来的：亮色立绘（白/浅粉）上也够暗，白字不会消失。
    canvas.alpha_composite(_bottom_scrim(CARD_W, 860, rgb), (0, CARD_H - 860))

    # ⚠️ 装饰画在独立透明层上，最后 alpha_composite 合成：Pillow 的 Draw 直写
    # 像素（带 alpha 时覆盖而非混合），半透明/阴影必须靠图层叠加。
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    date_f = load_font(17, "soft", data_dir)
    meta_f = load_font(19, "body", data_dir)
    bio_f = load_font(23, "body", data_dir)
    brow_f = load_font(20, "body", data_dir)

    bottom = CARD_H - 58

    # ---- 第 7 层（最淡）：日期 + 重抽标 ----
    draw.text((_PAD_X, bottom - date_f.size), f"{date_str}",
              font=date_f, fill=(255, 255, 255, 140))
    if rerolled:
        rt = "重抽"
        tw = draw.textlength(rt, font=date_f)
        bx0 = CARD_W - _PAD_X - tw - 26
        draw.rounded_rectangle((bx0, bottom - date_f.size - 6,
                                CARD_W - _PAD_X, bottom + 7),
                               radius=13, fill=(255, 255, 255, 32),
                               outline=(255, 255, 255, 118), width=1)
        draw.text((bx0 + 13, bottom - date_f.size), rt, font=date_f,
                  fill=(255, 255, 255, 232))
    bottom -= date_f.size + 16

    # ---- 第 6 层：统计行（档位色圆点 + 小字） ----
    stats = f"今日同款 {max(1, same_count)} 人 · 第 {max(1, total_count)} 次抽到她"
    dot_r = 3
    dot_cy = bottom - meta_f.size // 2
    draw.ellipse((_PAD_X, dot_cy - dot_r, _PAD_X + dot_r * 2, dot_cy + dot_r),
                 fill=(*t_rgb, 230))
    _mixed(layer, (_PAD_X + dot_r * 2 + 11, bottom - meta_f.size), stats, meta_f,
           None, (255, 255, 255, 170))
    bottom -= meta_f.size + 22

    # ---- 第 5 层：印象语（左侧档位色引文竖线 + 漫画体） ----
    quote = str(entry.get("quote") or "").strip()
    if quote:
        qf, qfb, qlines = _quote_layout(quote, data_dir, CARD_W - _PAD_X * 2 - 22)
        step = int(qf.size * 1.46)
        block_h = step * (len(qlines) - 1) + qf.size
        draw.rounded_rectangle((_PAD_X, bottom - block_h - 6, _PAD_X + 3, bottom + 4),
                               radius=2, fill=(*t_rgb, 236))
        ty = bottom
        for line in reversed(qlines):
            _mixed(layer, (_PAD_X + 19, ty - qf.size), line, qf, qfb,
                   (255, 255, 255, 244), shadow=(0, 0, 0, 120))
            ty -= step
        bottom -= block_h + 32

    # ---- 第 4 层：档位色分隔短线（把「简介」与「印象语」分成两层） ----
    draw.rounded_rectangle((_PAD_X, bottom - 2, _PAD_X + 42, bottom + 1),
                           radius=2, fill=(*t_rgb, 215))
    bottom -= 26

    # ---- 第 3 层：一句话简介（profile.tagline 优先 → bio → 身份/称号） ----
    # v0.45.1：新增 tagline——卡面专用的一行字（bio 常常是百科式长句，印上去会
    # 挤成两行小字）。没填就退回原来的 bio / title，旧数据渲染不变。
    profile = entry.get("profile") if isinstance(entry.get("profile"), dict) else {}
    bio = str(profile.get("tagline") or profile.get("bio")
              or profile.get("title") or "").strip()
    if bio:
        blines = _wrap(bio, bio_f, CARD_W - _PAD_X * 2, max_lines=2)
        step = int(bio_f.size * 1.48)
        ty = bottom
        for line in reversed(blines):
            _mixed(layer, (_PAD_X, ty - bio_f.size), line, bio_f, None,
                   (255, 255, 255, 200), shadow=(0, 0, 0, 100))
            ty -= step
        bottom -= step * (len(blines) - 1) + bio_f.size + 16

    # ---- 第 2 层：角色名（漫画体大标题，超长自动缩字号） ----
    name = str(entry.get("name") or "")
    name_max = CARD_W - _PAD_X * 2 - _SEAL - 34
    nsize = 78
    nf = load_font(nsize, style="comic", data_dir=data_dir)
    nfb = load_font(nsize, style="name", data_dir=data_dir)
    while nsize > 44 and text_width(name, nf, nfb) > name_max:
        nsize -= 4
        nf = load_font(nsize, style="comic", data_dir=data_dir)
        nfb = load_font(nsize, style="name", data_dir=data_dir)
    _mixed(layer, (_PAD_X, bottom - nsize), name, nf, nfb, (255, 255, 255, 252),
           shadow=(0, 0, 0, 150))

    # ---- 第 1 层（点睛）：档位印章，落在名字右侧 ----
    seal = _tier_seal(_SEAL, str(entry.get("tier") or ""), t_rgb, data_dir)
    sx = CARD_W - _PAD_X - _SEAL - 16
    # 印章视觉中心对齐名字的字面中心（CJK 字面中心≈基线上下 0.45em 处）
    sy = int(bottom - nsize * 0.45 - seal.height / 2 + 16)
    shadow = Image.new("RGBA", seal.size, (0, 0, 0, 0))
    shadow.putalpha(seal.getchannel("A").point(lambda a: int(a * 0.45)))
    layer.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(9)), (sx, sy + 7))
    layer.alpha_composite(seal, (sx, sy))
    bottom -= nsize + 16

    # ---- 第 0 层（最上，最小字）：作品眉标（档位色竖线 + 小字，字距略开） ----
    series = str(entry.get("series") or "").strip()
    cat = CATEGORY_LABELS.get(str(entry.get("category") or "").strip().lower(), "")
    brow = "  ·  ".join([x for x in (series, cat) if x])
    if brow:
        draw.rounded_rectangle((_PAD_X, bottom - 17, _PAD_X + 3, bottom),
                               radius=2, fill=(*t_rgb, 240))
        _mixed(layer, (_PAD_X + 15, bottom - brow_f.size), brow, brow_f, None,
               _tint(t_rgb, 0.30) + (240,), spacing=1.2)

    # ---- 收边：1px 半透明细线（替代原来的 14px 档位色宽边框） ----
    hair = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(hair).rounded_rectangle(
        (1, 1, CARD_W - 2, CARD_H - 2), radius=_CARD_RADIUS - 1,
        outline=(255, 255, 255, 54), width=1)
    layer = Image.alpha_composite(layer, hair)

    # 圆角卡片：合成后统一裁圆角（溢出的阴影一并裁掉）
    mask = Image.new("L", (CARD_W, CARD_H), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, CARD_W - 1, CARD_H - 1),
                                           radius=_CARD_RADIUS, fill=255)
    out = Path(out_path) if out_path else src.with_name(f"card_{src.stem}.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.alpha_composite(layer)  # 装饰层真混合（半透明/阴影/印章都靠它）
    canvas.putalpha(mask)
    canvas.save(out, "PNG", optimize=True)
    return str(out)


def sample_card_demo(portrait_path, out_path) -> str:
    """测试/预览用：用占位立绘渲染一张示例卡（初音未来配色），返回输出路径。"""
    entry = {
        "name": "初音未来", "series": "VOCALOID", "category": "anime",
        "tier": "S", "color": "#39C5BB",
        "profile": {"bio": "双马尾的电子歌姬，用歌声把世界连在一起。"},
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
