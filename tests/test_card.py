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

    # v0.45.0 版式：去宽边框，立绘满版（用户：金边没必要）
    with Image.open(out) as im:
        rgba = im.convert("RGBA")
        edge = rgba.getpixel((4, 300))      # 立绘区：无遮罩、无边框，应与立绘同色
        corner = rgba.getpixel((0, 0))
    check("立绘满版（边缘不再有档位色宽边框）", edge[:3] == (120, 190, 200))
    check("圆角外像素透明", corner[3] == 0)

    # 档位印章（替代五角星）：四档底色互不相同，S 为金色系
    seals = {}
    for t in ("S", "A", "B", "C"):
        seal = card._tier_seal(92, t, card.TIER_COLORS[t])
        seals[t] = seal.convert("RGB").getpixel((62, 96))
    check("S/A/B/C 印章底色互不相同", len(set(seals.values())) == 4)
    check("S 印章是金色系", seals["S"][0] > 140 and seals["S"][0] > seals["S"][2])

    # 一句话简介（profile.bio）真的进排版：加与不加输出必然不同
    plain = td / "no_bio.png"
    with_bio = td / "bio.png"
    card.render_card(portrait, ENTRY, date_str="2026-11-11", out_path=plain)
    card.render_card(portrait, dict(ENTRY, profile={"bio": "测试用的一句话简介。"}),
                     date_str="2026-11-11", out_path=with_bio)
    check("profile.bio 参与排版", plain.read_bytes() != with_bio.read_bytes())

    # 漫画体缺字回落：生僻日文汉字没有字形，必须回落而不是豆腐块
    comic = card.load_font(40, style="comic")
    body = card.load_font(40, style="name")
    check("漫画体覆盖常用汉字", card._has_glyph(comic, "初"))
    if comic is not body:
        check("漫画体缺字可识别（凪）", not card._has_glyph(comic, "凪"))
    rare = td / "rare.png"
    card.render_card(portrait, dict(ENTRY, name="凪雫栞"), date_str="2026-11-11",
                     out_path=rare)
    check("生僻字名字仍能渲染", Path(rare).stat().st_size > 1000)

print("test_card: ALL OK")
