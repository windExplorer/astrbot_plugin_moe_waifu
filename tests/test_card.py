# -*- coding: utf-8 -*-
"""card.py 渲染自检（可直接运行，退出码即结论；输出临时文件，不落仓库）。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

import card  # noqa: E402


def check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    print(f"ok: {name}")


ENTRY = {
    "name": "初音未来", "series": "VOCALOID", "category": "anime",
    "tier": "S", "color": "#39C5BB",
    "quote": "初次见面，我是初音未来！从今天起，就是你的专属歌姬了哦。",
}

with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    portrait = td / "portrait.png"
    Image.new("RGB", (card.CARD_W, card.CARD_H), (120, 190, 200)).save(portrait)

    out = card.render_card(portrait, ENTRY, date_str="2026-11-11",
                           same_count=3, total_count=12, rerolled=True,
                           out_path=td / "card.png")
    with Image.open(out) as img:  # with 块内用完即关，否则 Windows 清不掉临时目录
        check("输出 700×1200", img.size == (card.CARD_W, card.CARD_H))
    check("重抽标渲染不报错", True)

    # 无图必须拒绝渲染（PRD 铁律 1：不存在无图版式）
    try:
        card.render_card(td / "missing.png", ENTRY, date_str="2026-11-11",
                         out_path=td / "bad.png")
        check("缺立绘应拒绝渲染", False)
    except FileNotFoundError:
        check("缺立绘拒绝渲染（硬依赖）", True)

    # 尺寸不合的立绘也能 cover-fit
    small = td / "small.png"
    Image.new("RGB", (512, 512), (200, 150, 170)).save(small)
    out2 = card.render_card(small, ENTRY, date_str="2026-11-11",
                            out_path=td / "card2.png")
    with Image.open(out2) as img2:
        check("异尺寸立绘 cover-fit", img2.size == (card.CARD_W, card.CARD_H))

    # 折行：长句两行加省略号
    lines = card._wrap("这是一段特别长的印象语" * 20, card.load_font(27), card.CARD_W - 92)
    check("折行不超过两行", len(lines) == 2 and lines[1].endswith("…"))

    # 档位色宽边框（v0.9.9）：四档位各有专属色，边框确实覆盖卡片边缘
    frame_px = {}
    for t in ("S", "A", "B", "C"):
        p = td / f"frame_{t}.png"
        card.render_card(portrait, dict(ENTRY, tier=t), date_str="2026-11-12",
                         out_path=p)
        with Image.open(p) as im:
            frame_px[t] = im.convert("RGB").getpixel((6, 600))
    check("S/A/B/C 边框颜色互不相同", len(set(frame_px.values())) == 4)
    with Image.open(out) as im:
        rgba = im.convert("RGBA")
        inside = rgba.getpixel((40, 600))
        corner = rgba.getpixel((0, 0))
    check("宽边框与画面内侧不同色", frame_px["S"][:3] != inside[:3])
    check("圆角外像素透明", corner[3] == 0)

print("test_card: ALL OK")
