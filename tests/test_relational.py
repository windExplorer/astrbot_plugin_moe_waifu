# -*- coding: utf-8 -*-
"""关系模型阶段 1 自检（可直接运行，退出码即结论；不联网、不依赖 astrbot）。

守卫点：四张主键表存在、迁移只纳入三游戏、幂等、全部图片拿到全局 asset_id、
语义化 char_id 保留、老库就地补列、状态接口可用。
"""
import asyncio
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pool  # noqa: E402
import store  # noqa: E402
import webui_api  # noqa: E402


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL: {name} {detail}")
        sys.exit(1)
    print(f"ok: {name}")


def E(cid, name, series):
    return {"id": cid, "name": name, "series": series,
            "tags_en": "1girl", "verified": True, "category": "game",
            "strategy": {"type": "base"}}


def dump(path, pack, entries):
    path.write_text(json.dumps({"pack": pack, "title": pack,
                                "enabled": True, "entries": entries},
                               ensure_ascii=False), encoding="utf-8")


with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    pools = root / "pools"
    pools.mkdir()
    dump(pools / "game.json", "game", [
        E("ganyu", "甘雨", "原神"),
        E("keqing", "刻晴", "原神"),
        E("verina", "维里奈", "鸣潮"),
        E("nanally_nte", "娜娜莉", "异环"),
        E("march_7th", "三月七", "崩坏：星穹铁道"),   # 不在首版范围
    ])
    dump(pools / "anime.json", "anime", [
        E("hatsune_miku", "初音未来", "VOCALOID"),     # 不在首版范围
    ])
    (pools / "broken.json").write_text("{ not json", encoding="utf-8")
    # 图片：立绘 + 参考图 + 卡片 + 试画产物
    for rel, raw in (("portraits/ganyu/0.png", b"portrait-a"),
                     ("refs/r1.png", b"ref-a"),
                     ("cards/c1.png", b"card-a"),
                     ("try_cache/t1.png", b"try-a")):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(raw)

    st = store.Store(root / "waifu.db")
    # 1) 四张表 + 主键
    tables = {r["name"] for r in st._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    check("四张新表都已建立",
          {"assets", "packs", "works", "characters"} <= tables, str(tables))
    for t in ("assets", "packs", "works", "characters"):
        cols = {r["name"]: r for r in st._conn.execute(
            f"PRAGMA table_info({t})")}
        check(f"{t} 有自增主键",
              any(v["pk"] and "INTEGER" in str(v["type"]).upper()
                  for v in cols.values()), str(cols))
    cols = {r["name"] for r in st._conn.execute("PRAGMA table_info(portraits)")}
    check("portraits 就地补了 asset_id 列", "asset_id" in cols)

    # 2) 立绘落库自动登记 assets
    st.add_portrait("ganyu", "portraits/ganyu/0.png", 123,
                    "2026-11-13", 0, "base")
    prow = st.portrait_by_file("portraits/ganyu/0.png")
    check("add_portrait 回填 asset_id", int(prow["asset_id"] or 0) > 0,
          str(dict(prow)))
    aid = int(prow["asset_id"])
    check("同路径复用同一个 asset_id",
          st.register_asset("cover", "portraits/ganyu/0.png") == aid)

    # 3) 迁移：只纳入三游戏，幂等
    r1 = st.migrate_relational(pools, root)
    check("首版只纳入三游戏 128 口径下的 4 条", r1["characters"] == 4, str(r1))
    check("只建立 3 个作品", r1["works"] == 3, str(r1))
    check("只有含三游戏角色的卷入库（空卷 / 纯非范围卷不留记录）",
          r1["packs"] == 1, str(r1))
    check("非范围作品被跳过", r1["skipped"] == 2, str(r1))
    r2 = st.migrate_relational(pools, root)
    check("重复迁移幂等（数量不变）",
          all(r2[k] == r1[k] for k in ("assets", "packs", "works", "characters")),
          f"{r1} -> {r2}")

    # 4) 全部图片都有全局编号
    assets = [dict(r) for r in st._conn.execute(
        "SELECT id, kind, path, sha256 FROM assets ORDER BY id")]
    kinds = {a["kind"] for a in assets}
    check("四类图片都登记进 assets",
          {"portrait", "ref", "card", "try"} <= kinds, str(kinds))
    check("assets.id 全局唯一", len({a["id"] for a in assets}) == len(assets))
    check("assets.path 唯一", len({a["path"] for a in assets}) == len(assets))
    check("参考图算了内容哈希",
          any(a["kind"] == "ref" and len(a["sha256"]) == 64 for a in assets),
          str(assets))
    check("立绘行全部有 asset_id",
          st.relational_status()["portraits_with_asset"]
          == st.relational_status()["portraits"])

    # 5) 语义化 char_id 保留 + 作品/卷外键
    chars = [dict(r) for r in st._conn.execute(
        "SELECT char_id, name, work_id, pack_id FROM characters")]
    ids = {c["char_id"] for c in chars}
    check("保留语义化 char_id", "ganyu" in ids and "nanally_nte" in ids,
          str(ids))
    check("非范围角色没混进来",
          "march_7th" not in ids and "hatsune_miku" not in ids, str(ids))
    check("角色都挂了作品 / 卷外键",
          all(c["work_id"] and c["pack_id"] for c in chars), str(chars))
    wnames = {r["name"] for r in st._conn.execute("SELECT name FROM works")}
    check("作品名即自然键", wnames == {"原神", "鸣潮", "异环"}, str(wnames))

    # 6) 状态接口
    class P:
        _store = st

    res = asyncio.run(webui_api.h_relational_status(P()))
    check("状态接口返回各表数量",
          res["status"] == "ok" and res["data"]["status"]["assets"] == len(assets),
          str(res))

    # 7) 老库：只有 portraits 旧表也能就地升级
    old_db = root / "old.db"
    con = sqlite3.connect(old_db)
    con.executescript(
        "CREATE TABLE portraits (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "char_id TEXT, file TEXT, seed INTEGER, date TEXT, seq INTEGER, "
        "strategy TEXT, created_at TEXT);")
    con.commit()
    con.close()
    st2 = store.Store(old_db)
    old_cols = {r["name"] for r in st2._conn.execute(
        "PRAGMA table_info(portraits)")}
    check("老库补 asset_id 列", "asset_id" in old_cols, str(old_cols))
    old_tables = {r["name"] for r in st2._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    check("老库新建四张表",
          {"assets", "packs", "works", "characters"} <= old_tables)
    st2.close()

    # 8) 读路径：load_packs 与卷 JSON 同构
    packs_db = st.load_packs()
    check("新表读出的卷数与库一致", len(packs_db) == r1["packs"],
          f"{len(packs_db)} vs {r1['packs']}")
    flat = [e for p in packs_db for e in p["entries"]]
    check("读出的角色数 = characters 表", len(flat) == r1["characters"],
          f"{len(flat)} vs {r1['characters']}")
    ganyu = next(e for e in flat if e["id"] == "ganyu")
    check("条目字段完整（作品/卷/分类/颜色/strategy）",
          ganyu["series"] == "原神" and ganyu["pack"] == "game"
          and ganyu["category"] == "game" and ganyu["color"]
          and isinstance(ganyu["strategy"], dict), str(ganyu))
    check("normalize_entry 吃得下新表条目",
          pool.normalize_entry(ganyu, "game")["id"] == "ganyu")

    # 9) 数据源钩子：优先新表，空表回退 JSON
    pool.set_packs_provider(st.load_packs)
    try:
        via_db = pool.load_packs(pools)
        check("读路径走新表", len(via_db) == len(packs_db), str(len(via_db)))
        pool.set_packs_provider(lambda: [])
        fallback = pool.load_packs(pools)
        check("空表回退 JSON（不会让抽卡没候选）", len(fallback) == 2,
              str(len(fallback)))
    finally:
        pool.set_packs_provider(None)

    # 10) 抽卡候选：切读后只含三游戏
    cands = pool.candidates_for(st.load_packs(), "", "", True)
    check("候选只剩三游戏 4 位",
          len(cands) == 4
          and all(c["series"] in {"原神", "鸣潮", "异环"} for c in cands),
          str([c["id"] for c in cands]))

    # 11) 单卷同步：改字段 + 删除卷里已消失的角色
    dump(pools / "game.json", "game", [
        {"id": "ganyu", "name": "甘雨改", "series": "原神",
         "tags_en": "1girl", "verified": True, "category": "game", "tier": "S",
         "strategy": {"type": "base"}},
        E("verina", "维里奈", "鸣潮"),
        E("nanally_nte", "娜娜莉", "异环"),
    ])
    rs = st.sync_pack_file(pools / "game.json")
    row = st._conn.execute(
        "SELECT name, tier FROM characters WHERE char_id='ganyu'").fetchone()
    check("同步更新了字段", row["name"] == "甘雨改" and row["tier"] == "S",
          str(dict(row)))
    check("同步删除了卷里已消失的角色",
          st._conn.execute(
              "SELECT 1 FROM characters WHERE char_id='keqing'").fetchone() is None)
    check("同步返回 kept / removed", rs["kept"] == 3 and rs["removed"] == 1,
          str(rs))
    check("非 scope 角色始终不入库",
          st._conn.execute(
              "SELECT 1 FROM characters WHERE char_id='march_7th'").fetchone() is None)
    check("读路径立刻看到改动",
          len([e for p in st.load_packs() for e in p["entries"]]) == 3)

    # 12) 跨卷重复 id：先同步的卷胜，遗留卷不能把角色抢过去
    dump(pools / "zolddup.json", "zolddup", [
        {"id": "ganyu", "name": "甘雨旧卷", "series": "原神",
         "tags_en": "1girl", "verified": False, "category": "game",
         "strategy": {}},
    ])
    st.sync_pack_file(pools / "zolddup.json")
    row = st._conn.execute(
        "SELECT pack_id FROM characters WHERE char_id='ganyu'").fetchone()
    game_pack = st._conn.execute(
        "SELECT id FROM packs WHERE name='game'").fetchone()
    check("跨卷重复不抢卷（先见者胜）", row["pack_id"] == game_pack["id"],
          str(dict(row)))
    check("遗留卷没有抢到角色",
          st._conn.execute(
              "SELECT COUNT(*) AS n FROM characters c JOIN packs p ON p.id=c.pack_id"
              " WHERE p.name='zolddup'").fetchone()["n"] == 0)

    # 13) 空卷（遗留 / 只有非 scope 角色）不保留卷记录，读路径也不返回
    dump(pools / "zempty.json", "zempty", [E("outsider", "外人", "VOCALOID")])
    rs2 = st.sync_pack_file(pools / "zempty.json")
    check("空卷同步后不留 packs 记录",
          st._conn.execute(
              "SELECT 1 FROM packs WHERE name='zempty'").fetchone() is None)
    check("空卷 sync 返回 pack_dropped", rs2["pack_dropped"] is True, str(rs2))
    check("读路径不含空卷", all(p["entries"] for p in st.load_packs()))
    st.close()

print("test_relational: ALL OK")
