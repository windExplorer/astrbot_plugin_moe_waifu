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

    # v0.54.5（用户红线）：卡面绝不画「重抽 / 首抽」标——rerolled 传 True/False
    # 必须输出完全相同的像素（参数只为兼容旧调用签名而保留）。
    r_on = td / "reroll_on.png"
    r_off = td / "reroll_off.png"
    card.render_card(portrait, ENTRY, date_str="2026-11-11", same_count=3,
                     total_count=12, rerolled=True, out_path=r_on)
    card.render_card(portrait, ENTRY, date_str="2026-11-11", same_count=3,
                     total_count=12, rerolled=False, out_path=r_off)
    check("卡面不画重抽标（rerolled 不影响像素）",
          r_on.read_bytes() == r_off.read_bytes())

    # v0.54.2：新增「今日第 N 个抽到她」——同一参数下给不给 today_rank 输出必然不同，
    # 且三段文字在窄卡上要和日期错开（缩字号兜底，不重叠）
    no_rank = td / "no_rank.png"
    with_rank = td / "with_rank.png"
    card.render_card(portrait, ENTRY, date_str="2026-11-11",
                     same_count=3, total_count=12, out_path=no_rank)
    card.render_card(portrait, ENTRY, date_str="2026-11-11",
                     same_count=3, total_count=12, today_rank=2,
                     out_path=with_rank)
    check("today_rank 真的进排版",
          no_rank.read_bytes() != with_rank.read_bytes())
    with Image.open(with_rank) as imr:
        check("带名次的卡仍是 700×1200", imr.size == (card.CARD_W, card.CARD_H))

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

    # v0.45.1：一句话简介（profile.tagline）优先于 bio——两者同时给时，卡面
    # 必须是 tagline 那一行（bio 只在没 tagline 时兜底）
    tag_line = td / "tagline.png"
    card.render_card(portrait, dict(ENTRY, profile={
        "tagline": "卡面专用的一句话。", "bio": "百科式简介，不该出现在这张卡上。"}),
        date_str="2026-11-11", out_path=tag_line)
    only_tag = td / "tagline_only.png"
    card.render_card(portrait, dict(ENTRY, profile={"tagline": "卡面专用的一句话。"}),
                     date_str="2026-11-11", out_path=only_tag)
    only_bio = td / "bio_only.png"
    card.render_card(portrait, dict(ENTRY, profile={"bio": "百科式简介，不该出现在这张卡上。"}),
                     date_str="2026-11-11", out_path=only_bio)
    check("profile.tagline 优先于 bio",
          tag_line.read_bytes() == only_tag.read_bytes()
          and tag_line.read_bytes() != only_bio.read_bytes())

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

    # ---- v0.47.0 版式 v5：页脚左右分栏 / 眉标只写作品 / 遮罩与印章位置可配 ----
    from PIL import ImageChops

    def diff_box(p1, p2):
        with Image.open(p1) as i1, Image.open(p2) as i2:
            return ImageChops.difference(i1.convert("RGB"),
                                         i2.convert("RGB")).getbbox()

    # 日期在右下角：只换日期文本，差异区必须落在右下
    d1 = td / "date_a.png"
    d2 = td / "date_b.png"
    card.render_card(portrait, ENTRY, date_str="2026-11-11", out_path=d1)
    card.render_card(portrait, ENTRY, date_str="2028-01-09", out_path=d2)
    box = diff_box(d1, d2)
    check("日期在右下角", bool(box) and box[0] > card.CARD_W // 2
          and box[1] > card.CARD_H - 90)

    # 统计在左下角：只改同款人数 / 次数
    s1 = td / "stat_a.png"
    s2 = td / "stat_b.png"
    card.render_card(portrait, ENTRY, date_str="2026-11-11",
                     same_count=1, total_count=2, out_path=s1)
    card.render_card(portrait, ENTRY, date_str="2026-11-11",
                     same_count=37, total_count=88, out_path=s2)
    box = diff_box(s1, s2)
    check("同款统计在左下角", bool(box) and box[2] < card.CARD_W // 2
          and box[1] > card.CARD_H - 90)

    # 眉标只写作品名：分类（动漫 / 游戏）不同不该影响卡面
    c1 = td / "cat_a.png"
    c2 = td / "cat_b.png"
    card.render_card(portrait, dict(ENTRY, category="anime"), date_str="2026-11-11",
                     out_path=c1)
    card.render_card(portrait, dict(ENTRY, category="game"), date_str="2026-11-11",
                     out_path=c2)
    check("眉标不再显示分类（只写作品）", c1.read_bytes() == c2.read_bytes())

    # 遮罩浓度可配：值越小，文字区越亮
    dark = td / "scrim_dark.png"
    light = td / "scrim_light.png"
    card.render_card(portrait, ENTRY, date_str="2026-11-11", scrim=130,
                     out_path=dark)
    card.render_card(portrait, ENTRY, date_str="2026-11-11", scrim=40,
                     out_path=light)
    with Image.open(dark) as im:
        dark_px = sum(im.convert("RGB").getpixel((350, 1000)))
    with Image.open(light) as im:
        light_px = sum(im.convert("RGB").getpixel((350, 1000)))
    check("card_scrim 能调浅遮罩", light_px > dark_px + 30)
    check("card_scrim 归一（越界不炸）",
          card.normalize_scrim("abc") == 1.0 and card.normalize_scrim(0) == 0.0
          and card.normalize_scrim(9999) == 1.6)

    # 印章位置可配：右上角必须有档位色块，默认（名字右侧）同一区域则没有
    tr = td / "seal_tr.png"
    nm = td / "seal_name.png"
    card.render_card(portrait, ENTRY, date_str="2026-11-11", seal_pos="top_right",
                     out_path=tr)
    card.render_card(portrait, ENTRY, date_str="2026-11-11", seal_pos="name",
                     out_path=nm)

    def _gold_pixels(p):
        with Image.open(p) as im:
            rgb = im.convert("RGB")
            n = 0
            for x in range(540, 690, 3):
                for y in range(20, 170, 3):
                    r, g, b = rgb.getpixel((x, y))
                    if r > 150 and b < 130 and g > 110:
                        n += 1
            return n

    check("印章可放右上角", _gold_pixels(tr) > 40)
    check("默认印章不在顶部", _gold_pixels(nm) < 10)
    check("印章位置配置归一（写错回落默认）",
          card.normalize_seal_pos("bottom_left") == "name"
          and card.normalize_seal_pos("TOP-RIGHT") == "top_right")

print("test_card: ALL OK")
