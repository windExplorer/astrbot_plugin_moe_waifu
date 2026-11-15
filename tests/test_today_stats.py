# -*- coding: utf-8 -*-
"""v0.54.2 卡面统计口径守卫（可直接运行，退出码即结论）。

覆盖三件事：
① `Store.today_char_stats` 的跨群/本群两种口径与「今日第几个抽到她」名次；
② 换老婆后旧角色的同款人数会把这个人排除（按当日最终结果计数）；
③ `update_draw_card` 的统计快照（卡面不刷新时用来判断要不要重渲染）。
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import store  # noqa: E402

TODAY = "2026-11-15"
FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"ok: {name}")
    else:
        FAILURES.append(f"{name} {detail}")
        print(f"FAIL: {name} {detail}")


def draw(st, uid, char_id, group_id, gname="", nonce=0, rerolled=False):
    st.upsert_draw(uid, TODAY, char_id, nonce, rerolled, f"nick-{uid}",
                   group_id, gname, "aiocqhttp", char_name=char_id)


with tempfile.TemporaryDirectory() as td:
    st = store.Store(Path(td) / "waifu.db")

    # 同一角色：u1 在群 100，u2 在群 200
    draw(st, "u1", "x", "100")
    draw(st, "u2", "x", "200")

    cnt, rank = st.today_char_stats("x", TODAY, "u1", "100", "all")
    check("跨群口径：两人都算同款", cnt == 2, cnt)
    check("跨群口径：u1 是第 1 个", rank == 1, rank)
    cnt2, rank2 = st.today_char_stats("x", TODAY, "u2", "200", "all")
    check("跨群口径：u2 是第 2 个", cnt2 == 2 and rank2 == 2, (cnt2, rank2))

    cnt, rank = st.today_char_stats("x", TODAY, "u1", "100", "group")
    check("本群口径：群 100 只有自己", (cnt, rank) == (1, 1), (cnt, rank))
    cnt, rank = st.today_char_stats("x", TODAY, "u2", "200", "group")
    check("本群口径：群 200 只有自己", (cnt, rank) == (1, 1), (cnt, rank))

    # 私聊（无群号）在本群口径下必须回落跨群，不能恒等于 1
    draw(st, "u3", "x", "")
    cnt, rank = st.today_char_stats("x", TODAY, "u3", "", "group")
    check("私聊回落跨群口径", cnt == 3 and rank >= 1, (cnt, rank))

    # 换老婆：u1 把 x 换成 y → x 的同款只剩 u2、u3
    draw(st, "u1", "y", "100", nonce=1, rerolled=True)
    cnt, rank = st.today_char_stats("x", TODAY, "u2", "200", "all")
    check("换签后旧角色不计入自己", cnt == 2, cnt)
    cnt_y, _ = st.today_char_stats("y", TODAY, "u1", "100", "all")
    check("换到的新角色计入同款", cnt_y == 1, cnt_y)

    # 统计快照落库
    st.update_draw_card("u2", TODAY, "/tmp/card.png", same_count=2,
                        today_rank=1, total_count=4)
    row = st.get_draw("u2", TODAY)
    check("统计快照落库", (row["same_count"], row["today_rank"],
                           row["total_count"]) == (2, 1, 4),
          (row["same_count"], row["today_rank"], row["total_count"]))
    check("card_path 仍会更新", row["card_path"] == "/tmp/card.png", row["card_path"])

    # 不传统计时不覆盖已有快照（工坊重渲染路径）
    st.update_draw_card("u2", TODAY, "/tmp/card2.png")
    row = st.get_draw("u2", TODAY)
    check("不传统计保留旧快照", (row["same_count"], row["today_rank"]) == (2, 1),
          (row["same_count"], row["today_rank"]))

    # v0.54.3：群名回填（老记录 group_name 空 → 工坊补名后两张表都更新）
    draw(st, "u4", "z", "999")
    n = st.set_group_name("999", "测试群")
    check("群名回填写回 draws", n >= 1 and st.get_draw("u4", TODAY)["group_name"] == "测试群",
          (n, st.get_draw("u4", TODAY)["group_name"]))
    ev_rows, _ev_total = st.list_events(200, 0)
    check("群名回写覆盖 draw_events",
          all(r.get("group_name") == "测试群" for r in ev_rows if r.get("group_id") == "999"),
          [(r.get("group_id"), r.get("group_name")) for r in ev_rows][:3])
    check("群名回填不覆盖已有名字",
          st.set_group_name("999", "另一个名字") >= 0
          and st.get_draw("u4", TODAY)["group_name"] == "测试群")

    check("user_char_count 可用（无记录=0）", st.user_char_count("u1", "y") == 0,
          st.user_char_count("u1", "y"))
    st.bump_user_char("u1", "y")
    check("user_char_count 自增", st.user_char_count("u1", "y") == 1,
          st.user_char_count("u1", "y"))
    st.close()

if FAILURES:
    print(f"test_today_stats: {len(FAILURES)} FAILED")
    sys.exit(1)
print("test_today_stats: ALL OK")
