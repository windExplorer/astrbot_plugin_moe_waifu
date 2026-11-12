# -*- coding: utf-8 -*-
"""抽签记录 / 统计 / 用户查询 / 调试 core 的真 SQL 守卫（可直接运行，退出码即结论）。

不启 quart、不装 astrbot：临时库 + SimpleNamespace 桩，直接喂 webui_api 的
_xxx_core。覆盖 v0.9.0 最容易出错的三件事：老库补列、重抽轨迹不丢、统计口径。
"""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import store  # noqa: E402
import webui_api as api  # noqa: E402

TODAY = "2026-11-12"


FAILURES = []


def check(name, cond, detail=""):
    """收集所有失败再统一退出：中途 sys.exit 会让临时库的 SQLite 连接来不及 close，
    Windows 上 TemporaryDirectory 清理会撞文件锁（假异常盖住真实断言）。"""
    if not cond:
        FAILURES.append(f"{name} {detail}")
        print(f"FAIL: {name} {detail}")
    else:
        print(f"ok: {name}")


def make_plugin(root: Path, st) -> SimpleNamespace:
    pools = root / "pools"
    pools.mkdir(parents=True, exist_ok=True)
    (pools / "anime.json").write_text(json.dumps({
        "pack": "anime", "title": "动漫卷", "enabled": True,
        "entries": [
            {"id": "a_char", "name": "小明", "series": "测试作品",
             "category": "anime", "tier": "S", "tags_en": "a, blue hair",
             "color": "#39C5BB", "verified": True},
            {"id": "b_char", "name": "小红", "series": "测试作品",
             "category": "anime", "tier": "A", "tags_en": "b, red hair",
             "color": "#D63384", "verified": True},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    cfg = {
        "timezone": "Asia/Shanghai", "salt": "test-salt",
        "group_pools": "", "allow_unverified": False,
        "reroll_daily_limit": 1, "portraits_per_char": 5,
        "draw_workflow": "", "draw_timeout": 120, "ui_grid_cols": 4,
        "draw_width": 700, "draw_height": 1200,
    }
    return SimpleNamespace(
        _store=st, _data_dir=root,
        config=cfg,
        _cfg=lambda key, default=None: cfg.get(key, default),
        _today=lambda: TODAY,
    )


def fresh(tmp: Path):
    db = tmp / "waifu.db"
    st = store.Store(db)
    return st, make_plugin(tmp, st)


def test_migration(tmp: Path):
    """老库（12 列 draws）不重建、补列、旧数据还在。"""
    db = tmp / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
    CREATE TABLE draws (
      user_id TEXT NOT NULL, date TEXT NOT NULL, char_id TEXT NOT NULL,
      nonce INTEGER NOT NULL DEFAULT 0, rerolled INTEGER NOT NULL DEFAULT 0,
      nickname TEXT, group_id TEXT, group_name TEXT, platform TEXT,
      card_path TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      PRIMARY KEY (user_id, date));
    INSERT INTO draws VALUES ('u1','2026-11-01','old_char',0,0,'老用户','g1','老群','aiocqhttp',
                              '', '2026-11-01T00:00:00Z','2026-11-01T00:00:00Z');
    """)
    conn.commit()
    conn.close()
    st = store.Store(db)
    cols = {r["name"] for r in st._conn.execute("PRAGMA table_info(draws)")}
    check("老库补列 5 个新字段",
          {"seed_hex", "char_name", "series", "portrait_file", "decision"} <= cols,
          str(sorted(cols)))
    row = st.get_draw("u1", "2026-11-01")
    check("老库旧数据保留", row and row["char_id"] == "old_char" and row["nickname"] == "老用户")
    check("新列默认空串不破坏旧行", row["char_name"] == "" and row["seed_hex"] == "")
    st.close()


def test_portraits_seq_and_meta(tmp: Path):
    """v0.15.0：单调 seq（删除不复用）+ prompt/theme 随档 + 老库补列。"""
    db = tmp / "waifu.db"
    st = store.Store(db)
    # 新库 portraits 自带 prompt/theme；对老库（无这两列）建库脚本 CREATE IF NOT
    # EXISTS 即新形状，真正的老库走 _migrate ALTER 补列——此处验证列存在即口径
    cols = {r["name"] for r in st._conn.execute("PRAGMA table_info(portraits)")}
    check("portraits 含 prompt/theme 列", {"prompt", "theme"} <= cols, str(sorted(cols)))
    n0 = st.next_portrait_seq("c1")
    check("空角色下一个序号 = 0", n0 == 0)
    st.add_portrait("c1", "portraits/c1/a.png", 111, "2026-11-13", 0, "base",
                    prompt="第一条提示词", theme="swimsuit")
    st.add_portrait("c1", "portraits/c1/b.png", 222, "2026-11-13", 1, "try",
                    prompt="第二条提示词", theme="")
    st.add_portrait("c2", "portraits/c2/x.png", 333, "2026-11-13", 0, "moved")
    check("seq 单调派发", st.next_portrait_seq("c1") == 2)
    # 修重复编号的关键场景：删「最老」行后 count 会回退到 1（与现存 seq=1 撞号），
    # MAX+1 不受影响；删最新行会复用号（该号已无行，不会出现可见重复）
    st.delete_portrait("portraits/c1/a.png")
    check("删老行后 seq 不回退复用（重复编号根因修复）", st.next_portrait_seq("c1") == 2)
    rows = st.list_portraits("c1")
    check("随档 prompt/theme 落库", rows[0]["prompt"] == "第二条提示词"
          and rows[0]["theme"] == "")
    check("moved 行 prompt 为空", st.list_portraits("c2")[0]["prompt"] is None)
    # 排序决胜：历史重复 seq 行按 created_at/id 稳定
    st.add_portrait("c1", "portraits/c1/c.png", 444, "2026-11-13", 1, "base")
    order = [r["file"] for r in st.list_portraits("c1")]
    check("重复 seq 行序稳定（created_at/id 决胜）",
          len(order) == 2 and order[0].endswith("b.png"), str(order))
    st.close()


def test_events_and_detail(tmp: Path):
    st, _ = fresh(tmp)
    st.upsert_draw("u1", TODAY, "a_char", 0, False, "阿明", "g1", "一群",
                   "aiocqhttp", seed_hex="aa11", char_name="小明",
                   series="测试作品", portrait_file="portraits/a_char/0.png",
                   decision="generate")
    st.update_draw_card("u1", TODAY, "/data/cards/u1.png")
    st.upsert_draw("u1", TODAY, "b_char", 1, True, "阿明", "g1", "一群",
                   "aiocqhttp", seed_hex="bb22", char_name="小红",
                   series="测试作品", portrait_file="portraits/b_char/0.png",
                   decision="from_pool")
    st.update_draw_card("u1", TODAY, "/data/cards/u1_r1.png")
    cur = st.get_draw("u1", TODAY)
    check("draws 覆盖为最后一次结果", cur["char_id"] == "b_char" and cur["nonce"] == 1)
    events, total = st.list_events(50, 0, "u1", TODAY)
    check("抽签流水 2 条（首抽 + 重抽）", total == 2 and len(events) == 2)
    check("重抽前的角色仍可追溯",
          {e["char_id"] for e in events} == {"a_char", "b_char"})
    kinds = {e["kind"] for e in events}
    check("事件类型 first / reroll", kinds == {"first", "reroll"})
    check("流水保留每次种子与立绘来源",
          {e["seed_hex"] for e in events} == {"aa11", "bb22"}
          and {e["portrait_file"] for e in events} ==
          {"portraits/a_char/0.png", "portraits/b_char/0.png"})
    by_id = {e["char_id"]: e for e in events}
    check("成卡路径只补到当天最后一条事件",
          by_id["a_char"]["card_path"] == "/data/cards/u1.png"
          and by_id["b_char"]["card_path"] == "/data/cards/u1_r1.png")
    detail = st.record_detail("u1", TODAY)
    check("单签详情含当前行 + 流水 + 任务",
          detail and detail["draw"]["char_id"] == "b_char"
          and len(detail["events"]) == 2 and isinstance(detail["jobs"], list))
    check("record_detail 无记录返回 None", st.record_detail("none", TODAY) is None)
    st.close()


def test_records_filters(tmp: Path):
    st, _ = fresh(tmp)
    data = [
        ("u1", "2026-11-10", "a_char", 0, "阿明", "g1", "一群", "aiocqhttp"),
        ("u1", "2026-11-11", "a_char", 0, "阿明", "g1", "一群", "aiocqhttp"),
        ("u1", TODAY, "b_char", 1, "阿明", "g1", "一群", "aiocqhttp"),
        ("u2", "2026-11-11", "b_char", 0, "小美", "", "", "aiocqhttp"),
        ("u2", TODAY, "a_char", 1, "小美", "", "", "telegram"),
        ("u3", TODAY, "a_char", 0, "阿猫", "g2", "二群", "telegram"),
    ]
    for uid, date, cid, nonce, nick, gid, gname, plat in data:
        st.upsert_draw(uid, date, cid, nonce, bool(nonce), nick, gid, gname, plat,
                       char_name="小明" if cid == "a_char" else "小红")
    all_rows = st.list_records(1, 20)
    check("记录分页统一形状", set(all_rows) == {"total", "page", "size", "rows"})
    check("记录总数 = 一人一天一行", all_rows["total"] == 6, str(all_rows["total"]))
    page1 = st.list_records(1, 2)
    check("分页 size/offset 生效",
          len(page1["rows"]) == 2 and page1["total"] == 6
          and len(st.list_records(3, 2)["rows"]) == 2
          and st.list_records(4, 2)["rows"] == [])
    check("关键词搜昵称", st.list_records(1, 20, q="小美")["total"] == 2)
    check("关键词搜角色名快照", st.list_records(1, 20, q="小红")["total"] == 2)
    check("日期区间含两端",
          st.list_records(1, 20, date_from="2026-11-11", date_to="2026-11-11")["total"] == 2)
    check("群筛选", st.list_records(1, 20, group_id="g1")["total"] == 3)
    check("私聊伪群筛选", st.list_records(1, 20, group_id="__private__")["total"] == 2)
    check("平台筛选", st.list_records(1, 20, platform="telegram")["total"] == 2)
    check("只看重抽", st.list_records(1, 20, rerolled="yes")["total"] == 2)
    check("角色筛选", st.list_records(1, 20, char_id="a_char")["total"] == 4)
    groups = st.list_groups()
    check("群下拉含私聊口径",
          any(g["group_id"] == "__private__" and g["n"] == 2 for g in groups))
    check("平台下拉去重", st.list_platforms() == ["aiocqhttp", "telegram"])
    st.close()


def test_users_and_streak(tmp: Path):
    check("连签：连续三天", store._streak_stats(
        ["2026-11-10", "2026-11-11", "2026-11-12"], "2026-11-12") == (3, 3))
    check("连签：今天还没抽，从昨天起算", store._streak_stats(
        ["2026-11-10", "2026-11-11"], "2026-11-12") == (2, 2))
    check("连签：断签归零但保留最长", store._streak_stats(
        ["2026-11-01", "2026-11-02", "2026-11-12"], "2026-11-12") == (1, 2))
    check("连签：跨月连续", store._streak_stats(
        ["2026-10-31", "2026-11-01", "2026-11-02"], "2026-11-02") == (3, 3))
    check("连签：空集合", store._streak_stats([], "2026-11-12") == (0, 0))

    st, _ = fresh(tmp)
    for d in ("2026-11-10", "2026-11-11", TODAY):
        st.upsert_draw("u1", d, "a_char", 0, False, "阿明", "g1", "一群",
                       "aiocqhttp", char_name="小明")
    st.upsert_draw("u1", TODAY, "b_char", 1, True, "阿明", "g1", "一群",
                   "aiocqhttp", char_name="小红")
    st.upsert_draw("u2", TODAY, "a_char", 0, False, "小美", "", "",
                   "telegram", char_name="小明")
    users = st.list_users(1, 24, today=TODAY)
    check("用户列表两人", users["total"] == 2, str(users["total"]))
    u1 = next(u for u in users["rows"] if u["user_id"] == "u1")
    check("用户汇总与连签", u1["draws"] == 3 and u1["chars"] == 2
          and u1["rerolls"] == 1 and u1["streak"] == 3, str(u1))
    check("用户搜索", st.list_users(1, 24, q="小美", today=TODAY)["total"] == 1)
    check("排序白名单回落",
          st.list_users(1, 24, order="BOGUS", today=TODAY)["total"] == 2)
    prof = st.user_profile("u1", TODAY, 3)
    check("单用户档案齐全",
          prof and set(("summary", "today", "top_chars", "history",
                        "events", "jobs", "calendar")) <= set(prof))
    check("档案今日签是重抽后的角色", prof["today"]["char_id"] == "b_char")
    check("档案连签与历史", prof["summary"]["streak"] == 3
          and len(prof["history"]) == 3 and len(prof["calendar"]) == 3)
    check("角色偏好 Top", prof["top_chars"][0]["n"] >= 1)
    check("不存在的用户返回 None", st.user_profile("ghost", TODAY) is None)
    st.close()


def test_stats(tmp: Path):
    st, _ = fresh(tmp)
    for d in ("2026-11-10", "2026-11-11", TODAY):
        st.upsert_draw("u1", d, "a_char", 0, False, "阿明", "g1", "一群",
                       "aiocqhttp", char_name="小明", decision="generate")
    st.upsert_draw("u2", TODAY, "a_char", 1, True, "小美", "", "",
                   "telegram", char_name="小明", decision="from_pool")
    st.log_job("a_char", TODAY, "generated", "ok", "u1", "base", 123, "", 2400, "x.png")
    st.log_job("a_char", TODAY, "generated", "error", "u2", "base", 124, "boom", 100, "")
    st.add_portrait("a_char", "portraits/a_char/0.png", 123, TODAY, 0, "base")
    data = st.stats_range("2026-11-10", TODAY)
    t = data["totals"]
    check("统计总量口径", t["draws"] == 4 and t["users"] == 2 and t["chars"] == 1, str(t))
    check("重抽率可算", t["rerolls"] == 1 and t["reroll_rate"] == 25.0)
    check("流水重抽数单独给（历史没流水也不混算）", t["events_rerolls"] == 1)
    check("出图成功率", t["jobs_total"] == 2 and t["jobs_ok"] == 1
          and t["success_rate"] == 50.0 and t["avg_ms"] == 2400)
    check("新用户口径（首次出现日落在区间内）", t["new_users"] == 2)
    check("立绘新增", t["portraits_added"] == 1)
    check("趋势补齐无数据的日子", len(data["trend"]) == 3, str(data["trend"]))
    check("趋势逐日聚合", [x["draws"] for x in data["trend"]] == [1, 1, 2])
    check("任务按日并入趋势", data["trend"][-1]["jobs_ok"] == 1
          and data["trend"][-1]["jobs_fail"] == 1)
    check("时段分布总和 = 抽签数", sum(h["n"] for h in data["hours"]) == 4)
    check("角色分布 Top", data["chars"][0]["n"] == 4)
    check("群分布含私聊", any(g["group_id"] == "__private__" for g in data["groups"]))
    check("立绘决策分布", {d["decision"] for d in data["decisions"]} ==
          {"generate", "from_pool"})
    st.close()


def test_debug_history(tmp: Path):
    st, _ = fresh(tmp)
    for i in range(210):
        st.debug_log("reset", "u1", f"action {i}")
    st.debug_log("query", "u9", "用户查询")
    count = st._conn.execute("SELECT COUNT(*) FROM debug_history").fetchone()[0]
    check("调试审计环形保留 200 条", count == 200, str(count))
    check("最近查询 uid", st.debug_recent() == ["u9"])
    audit = st.debug_audit(300)
    check("审计表不含 query 噪声", all(r["kind"] != "query" for r in audit))
    st.close()


def test_cores(tmp: Path):
    """webui_api 的 core 不启 quart 也能跑真逻辑。"""
    st, plugin = fresh(tmp)
    # 先用模拟抽签决定「真实链路会抽到谁」，再按这个结果落库：
    # 这样 matches_current 测的是真一致性，而不是恰好撞上手写的假数据。
    preview = api._debug_simulate_core(plugin, "u1", TODAY, 0, "test-salt", "")
    picked = preview["char"]
    st.upsert_draw("u1", TODAY, picked["char_id"], 0, False, "阿明", "g1", "一群",
                   "aiocqhttp", seed_hex=preview["seed_hex"],
                   char_name=picked["char_name"], decision="generate")
    st.update_draw_card("u1", TODAY, str(tmp / "cards" / "u1.png"))
    rec = api._records_core(plugin, {"page": "1", "size": "20"})
    check("records core 回填角色名与颜色",
          rec["total"] == 1 and rec["rows"][0]["char_color"] == picked["color"])
    stats = api._stats_core(plugin, 7, "2026-11-10", TODAY)
    check("stats core 区间生效",
          stats["range"] == {"from": "2026-11-10", "to": TODAY}
          and stats["totals"]["draws"] == 1)
    user = api._user_core(plugin, "u1", "3")
    check("user core found",
          user["found"] is True and user["today"]["char_name"] == picked["char_name"])
    check("user core 查无此人安全降级", api._user_core(plugin, "ghost")["found"] is False)
    state = api._debug_state_core(plugin, "u1")
    check("debug state 配置快照与盐脱敏",
          state["config"]["salt"] == "te***t" and state["nonce"] == 0
          and state["pool"]["candidates"] == 2, str(state["config"]["salt"]))
    sim0 = api._debug_simulate_core(plugin, "u1", TODAY, 0, "test-salt", "")
    check("模拟抽签只读且给出完整链路",
          sim0["ok"] and sim0["readonly"] and sim0["candidates"] == 2
          and len(sim0["seed_hex"]) == 64 and sim0["decision"] in
          ("generate", "from_pool", "reuse_today"))
    check("模拟结果与实际签一致", sim0["matches_current"] is True)
    others = [api._debug_simulate_core(plugin, "u1", TODAY, n, "test-salt", "")
              for n in range(1, 8)]
    check("换 nonce 能预览不同结果",
          any(s["char"]["char_id"] != sim0["char"]["char_id"] for s in others))
    check("模拟抽签不写库（流水行数不变）",
          st.list_events(50, 0)[1] == 1)
    check("未绑定群回落全部启用卷",
          api._debug_simulate_core(plugin, "u1", TODAY, 0, "test-salt",
                                   "no_such_group")["candidates"] == 2)
    empty_root = tmp / "empty_pools"
    (empty_root / "pools").mkdir(parents=True, exist_ok=True)
    empty_plugin = make_plugin(empty_root, st)
    (empty_root / "pools" / "anime.json").unlink()
    empty = api._debug_simulate_core(empty_plugin, "u1", TODAY, 0, "test-salt", "")
    check("空角色库安全降级", empty["ok"] is False and empty["candidates"] == 0)
    check("模拟结果给出抽取范围（该群绑定卷 ∩ verified）",
          "scope" in sim0 and sim0["scope"]["group_packs"] is None
          and sim0["scope"]["allow_unverified"] is False
          and "anime" in sim0["scope"]["enabled_packs"])
    plugin.config["group_pools"] = "123456=anime"
    bound_sim = api._debug_simulate_core(plugin, "u1", TODAY, 0, "test-salt", "123456")
    check("绑定群限定到指定卷",
          bound_sim["scope"]["group_packs"] == ["anime"] and bound_sim["candidates"] == 2)
    unbound_sim = api._debug_simulate_core(plugin, "u1", TODAY, 0, "test-salt", "999999")
    check("未列出的群回落全部启用卷", unbound_sim["scope"]["group_packs"] is None)
    st.close()


def test_discover_add_core(tmp: Path):
    """v0.18.0：作品发现入库 core——草稿 unverified、跨卷查重、坏条目丢弃。"""
    st, plugin = fresh(tmp)
    rows = [
        {"id": "new1", "name": "草稿甲", "series": "新作", "category": "gal",
         "tier": "A", "tags_en": "black hair, purple eyes, miko", "color": "#B28DCC"},
        {"id": "a_char", "name": "重复", "series": "新作", "tier": "C", "tags_en": "dup"},
        {"id": "bad", "name": "缺tags", "series": "新作"},
    ]
    r = api._discover_add_core(plugin, "anime", rows)
    check("草稿入库计数（含跨卷查重）", r == {"added": 1, "dup": 1}, str(r))
    loaded = api._load_pack_file(plugin, "anime")
    data = json.loads(loaded[0].read_text(encoding="utf-8"))
    new_row = [e for e in data["entries"] if e["id"] == "new1"]
    check("入库条目存在且 unverified", new_row and new_row[0]["verified"] is False)
    r2 = api._discover_add_core(plugin, "不存在卷", rows)
    check("卷不存在报错", "error" in r2)
    st.close()


def main():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "mig").mkdir(parents=True, exist_ok=True)
        print("--- migration ---")
        test_migration(root / "mig")
    for name, fn in (("events_and_detail", test_events_and_detail),
                     ("records_filters", test_records_filters),
                     ("users_and_streak", test_users_and_streak),
                     ("stats", test_stats),
                     ("debug_history", test_debug_history),
                     ("portraits_seq_meta", test_portraits_seq_and_meta),
                     ("discover_add", test_discover_add_core),
                     ("cores", test_cores)):
        with tempfile.TemporaryDirectory() as td:
            print(f"--- {name} ---")
            fn(Path(td))
    if FAILURES:
        print(f"test_records: {len(FAILURES)} FAILED")
        sys.exit(1)
    print("test_records: ALL OK")


if __name__ == "__main__":
    main()
