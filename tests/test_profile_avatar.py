# -*- coding: utf-8 -*-
"""角色资料 / 评级 / 头像（v0.31.0）纯函数与真 SQL 守卫（可直接运行，退出码即结论）。

覆盖四类最容易回归的点：
1. 新条目字段（profile / avatar / added_at）的「编辑表单不托管」保护——
   merge_saved_entry 必须原样保留旧值（v0.9.10 同类事故：整体替换清空资产）；
2. LLM 资料建议的 JSON 抠取与清洗（白名单 / 限长 / 空值剔除 / 坏输入兜底）；
3. 头像裁剪与提示词按底模风格分流（固有特征必须点名——头像最怕丢耳朵尾巴）；
4. store.char_summary / stats_range 逐日耗时聚合的真 SQL 口径。
"""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import avatars  # noqa: E402
import pool  # noqa: E402
import store  # noqa: E402
import webui_api as api  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if not cond:
        FAILURES.append(f"{name} {detail}")
        print(f"FAIL: {name} {detail}")
    else:
        print(f"ok: {name}")


def test_normalize_and_merge():
    raw = {"id": "a", "name": "小明", "tags_en": "a, blue hair",
           "profile": {"bio": "测试简介", "birthday": "8月31日"},
           "avatar": "avatars/a.png", "added_at": "2026-11-14"}
    e = pool.normalize_entry(raw, "anime")
    check("normalize 透传 profile/avatar/added_at",
          e.get("profile", {}).get("bio") == "测试简介"
          and e.get("avatar") == "avatars/a.png"
          and e.get("added_at") == "2026-11-14")
    check("normalize profile 非法形态回落空 dict",
          pool.normalize_entry({"id": "b", "name": "x", "tags_en": "t",
                                "profile": "坏数据", "avatar": 123}, "anime")
          == pool.normalize_entry({"id": "b", "name": "x", "tags_en": "t",
                                   "profile": {}, "avatar": "123"}, "anime")
          or (pool.normalize_entry({"id": "b", "name": "x", "tags_en": "t",
                                    "profile": "坏数据"}, "anime")["profile"] == {}
              and pool.normalize_entry({"id": "b", "name": "x", "tags_en": "t",
                                        "profile": "坏数据"}, "anime")["avatar"] == ""))
    # 编辑表单不托管这三个字段：merge 必须保住旧值（防 v0.9.10 同类事故）
    form_entry = {"id": "a", "name": "小明（改名）", "tags_en": "a, blue hair",
                  "strategy": {"type": "base"}, "profile": {}, "avatar": "",
                  "added_at": ""}
    merged = pool.merge_saved_entry(raw, pool.normalize_entry(form_entry, "anime"))
    check("merge 保留 profile / avatar / added_at 旧值",
          merged.get("profile", {}).get("bio") == "测试简介"
          and merged.get("avatar") == "avatars/a.png"
          and merged.get("added_at") == "2026-11-14")
    check("merge 表单字段照常覆盖",
          merged.get("name") == "小明（改名）")


def test_sanitize_profile():
    sp = api._sanitize_profile({
        "bio": "  简介  ", "birthday": "8月31日", "unknown_key": "x",
        "cv": "", "height": 157, "memo": "长" * 600,
    })
    check("sanitize 白名单 + 空值剔除 + 字符串化",
          set(sp.keys()) == {"bio", "birthday", "height", "memo"}
          and sp["bio"] == "简介" and sp["height"] == "157")
    check("sanitize 限长 500", len(sp["memo"]) == 500)
    check("sanitize 非法输入兜底",
          api._sanitize_profile(None) == {} and api._sanitize_profile("x") == {})


def test_extract_json_object():
    check("extract 纯 JSON",
          api._extract_json_object('{"bio":"a"}') == {"bio": "a"})
    check("extract 容围栏与废话",
          api._extract_json_object('好的：```json\n{"bio":"a","cv":"b"}\n```')
          == {"bio": "a", "cv": "b"})
    check("extract 值内花括号/引号转义",
          api._extract_json_object('{"bio":"有 { 括号 \\" 引用"}')["bio"]
          == '有 { 括号 " 引用')
    check("extract 坏输入返回空 dict",
          api._extract_json_object("") == {}
          and api._extract_json_object("没有对象") == {}
          and api._extract_json_object('{"broken": ') == {})
    check("extract 非 dict 丢弃",
          api._extract_json_object('[1,2]') == {})


def test_avatars_helpers():
    check("safe_avatar_name 过滤非法字符",
          avatars.safe_avatar_name("a/b\\c d") == "a_b_c_d")
    check("avatar_rel 形状", avatars.avatar_rel("a") == "avatars/a.png")
    with tempfile.TemporaryDirectory() as tmp:
        from PIL import Image

        tmp_p = Path(tmp)
        # 竖版 300×500：裁上偏移方形 300×300 → 缩到 512
        src_p = tmp_p / "p.png"
        Image.new("RGB", (300, 500), (255, 0, 0)).save(str(src_p))
        dst_p = tmp_p / "out1.png"
        w, h = avatars.crop_square(src_p, dst_p)
        with Image.open(str(dst_p)) as im:
            check("竖版裁方形 + 512 归一", (w, h) == (512, 512)
                  and im.size == (512, 512))
        # 横版 500×300：水平居中裁
        src_l = tmp_p / "l.png"
        Image.new("RGB", (500, 300), (0, 255, 0)).save(str(src_l))
        w2, h2 = avatars.crop_square(src_l, tmp_p / "out2.png")
        check("横版裁方形", (w2, h2) == (512, 512)
              and (tmp_p / "out2.png").is_file())
        # 小图放大到 512
        src_s = tmp_p / "s.png"
        Image.new("RGB", (100, 100), (0, 0, 255)).save(str(src_s))
        avatars.crop_square(src_s, tmp_p / "out3.png")
        with Image.open(tmp_p / "out3.png") as im:
            check("小图放大 512", im.size == (512, 512))
        # 偏上裁剪确实取到上部像素：上半红下半蓝的竖图，裁完主体应为红
        src_g = tmp_p / "g.png"
        gimg = Image.new("RGB", (200, 600), (0, 0, 255))
        for y in range(0, 300):
            for x in range(0, 200):
                gimg.putpixel((x, y), (255, 0, 0))
        gimg.save(str(src_g))
        avatars.crop_square(src_g, tmp_p / "out4.png")
        with Image.open(tmp_p / "out4.png") as im:
            px = im.getpixel((256, 20))
            check("竖版偏上裁剪（上半部像素保留）", px[0] > 200 and px[2] < 60)


def test_avatar_prompt():
    entry = {"name": "狐娘", "tags_en": "1girl, fox ears, tail, blue hair"}
    d = avatars.build_avatar_prompt(entry, "danbooru")
    check("danbooru 头像提示词带构图词",
          "portrait" in d and "close-up" in d and "simple background" in d
          and "fox ears" in d)
    q = avatars.build_avatar_prompt(entry, "qwen")
    check("qwen 头像提示词中文 + 固有特征点名",
          "头部与肩部" in q and "尾巴" in q and "狐娘" in q)
    n = avatars.build_avatar_prompt(entry, "natural")
    check("natural 头像提示词英文 caption + 特征",
          "portrait" in n and "keep" in n and "fox ears" in n)


def test_store_summary_and_trend():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "waifu.db"
        st = store.Store(db)
        # draws：走 store 自己的写入口（列是迁移补出来的，别手写 INSERT）
        st.upsert_draw("u1", "2026-11-01", "a_char", 0, False, nickname="n",
                       group_id="g", group_name="群", platform="qq",
                       char_name="小明", decision="from_pool")
        st.upsert_draw("u2", "2026-11-03", "a_char", 0, False, nickname="m",
                       group_id="g", group_name="群", platform="qq",
                       char_name="小明", decision="generate")
        st.add_portrait("a_char", "portraits/a_char/x.png", 1, "2026-11-01", 0,
                        "base", duration_ms=61000)
        st.add_portrait("a_char", "portraits/a_char/y.png", 2, "2026-11-03", 1,
                        "base", duration_ms=39000)
        st._conn.commit()
        s = st.char_summary("a_char")
        check("char_summary 口径",
              s["first_draw"] == "2026-11-01" and s["last_draw"] == "2026-11-03"
              and s["draw_count"] == 2 and s["portrait_count"] == 2
              and s["portrait_avg_ms"] == 50000)
        check("char_summary 空角色不炸",
              st.char_summary("nobody")["draw_count"] == 0
              and st.char_summary("nobody")["portrait_avg_ms"] == 0)
        # stats_range 逐日 avg_ms（draw_jobs 成功项）
        st.log_job("a_char", "2026-11-01", "refill", "ok", "workshop", "base",
                   1, "", 30000, "p1.png")
        st.log_job("a_char", "2026-11-01", "refill", "ok", "workshop", "base",
                   2, "", 50000, "p2.png")
        st.log_job("a_char", "2026-11-02", "try", "error", "workshop", "base",
                   3, "boom", 1000, "")
        st._conn.commit()
        res = st.stats_range("2026-11-01", "2026-11-30", "Asia/Shanghai")
        trend = {t["date"]: t for t in res["trend"]}
        check("trend 逐日 avg_ms（成功项均值）",
              trend["2026-11-01"]["avg_ms"] == 40000
              and trend["2026-11-02"]["avg_ms"] == 0
              and trend["2026-11-03"]["avg_ms"] == 0)
        check("trend totals 平均耗时口径不变",
              int(res["totals"]["avg_ms"]) == 40000)
        st.close()


def test_backfill_duration():
    """v0.33.1：历史立绘耗时回填——draw_jobs 的 (char_id, seed) 匹配。"""
    with tempfile.TemporaryDirectory() as tmp:
        st = store.Store(Path(tmp) / "waifu.db")
        st.add_portrait("a", "portraits/a/1.png", 111, "2026-11-01", 0, "base")
        st.add_portrait("a", "portraits/a/2.png", 222, "2026-11-02", 1, "base",
                        duration_ms=5000)
        st.add_portrait("a", "portraits/a/3.png", 333, "2026-11-03", 2, "base")
        st.log_job("a", "2026-11-01", "refill", "ok", "workshop", "base",
                   111, "", 42000, "")
        st.log_job("a", "2026-11-01", "try", "ok", "workshop", "base",
                   111, "", 38000, "")   # 同键两条成功任务 → 取 MIN
        st.log_job("a", "2026-11-02", "try", "error", "workshop", "base",
                   222, "", 9000, "")    # 失败任务不参与回填
        st.log_job("b", "2026-11-01", "refill", "ok", "workshop", "base",
                   111, "", 30000, "")   # 别的角色不串用
        st._conn.commit()
        st._backfill_portrait_durations()
        rows = {r["file"]: r["duration_ms"] for r in st._conn.execute(
            "SELECT file, duration_ms FROM portraits")}
        check("回填：0 值立绘从 draw_jobs 匹配（同键多条取最小）",
              rows["portraits/a/1.png"] == 38000)
        check("回填：已有值不覆盖", rows["portraits/a/2.png"] == 5000)
        check("回填：无匹配任务的行保持 0（非生成路径）",
              rows["portraits/a/3.png"] == 0)
        st.close()


def test_backfill_ref_duration():
    """v0.33.2：已生成参考图的耗时回填——ref_meta.seed 匹配 draw_jobs。"""
    with tempfile.TemporaryDirectory() as tmp:
        st = store.Store(Path(tmp) / "waifu.db")
        st.set_ref_meta("refs/gen_1.png", seed=777, source="try")     # duration 0
        st.set_ref_meta("refs/gen_2.png", seed=888, source="try")
        st.log_job("a", "2026-11-01", "try", "ok", "workshop", "base",
                   777, "", 41000, "")
        st._conn.commit()
        st._backfill_ref_durations()
        metas = st.get_ref_meta(["refs/gen_1.png", "refs/gen_2.png"])
        check("参考图回填：seed 匹配到任务耗时",
              metas["refs/gen_1.png"]["duration_ms"] == 41000)
        check("参考图回填：无匹配任务的行保持 0",
              metas["refs/gen_2.png"]["duration_ms"] == 0)
        st.close()


def main():
    test_normalize_and_merge()
    test_sanitize_profile()
    test_extract_json_object()
    test_avatars_helpers()
    test_avatar_prompt()
    test_store_summary_and_trend()
    test_backfill_duration()
    test_backfill_ref_duration()
    if FAILURES:
        print(f"\ntest_profile_avatar: {len(FAILURES)} FAILED")
        sys.exit(1)
    print("\ntest_profile_avatar: ALL OK")


if __name__ == "__main__":
    main()
