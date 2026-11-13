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
    check("两个合法卷入库", r1["packs"] == 2, str(r1))
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
    st.close()

print("test_relational: ALL OK")
