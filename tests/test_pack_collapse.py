# -*- coding: utf-8 -*-
"""卷收敛 / 类型注册表 / 标签过滤守卫（v0.68.0，可直接运行，退出码即结论）。

用户口径：①卷收敛为「类型」，类型可自定义添加（内置 动漫/游戏/其他）；
②gal 归入游戏；③标签替代卷的启停粒度（停用标签的角色不入抽卡）；
④作品（series）永不受限——新表 scope 门禁移除，任何新作品都可见。
守卫点：
  ① migrate_pack_collapse：非标准卷按条目类型并入 + gal→game + 回收站备份 +
    幂等；同 id 冲突保留资产分高的一份；
  ② 类型注册表：默认三类型、添加 / 改名 / 删除（空类型才可删、内置不可动）；
  ③ store.rename_pack / delete_pack / prune_packs；
  ④ candidates_for 的 disabled_tags 过滤。
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pool  # noqa: E402
import store  # noqa: E402
import webui_api as api  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if not cond:
        FAILURES.append(name)
        print(f"FAIL: {name} {detail}")
    else:
        print(f"ok: {name}")


def dump(path: Path, pack: str, entries: list, title: str = "",
         enabled: bool = True) -> None:
    data = {"pack": pack, "enabled": enabled, "entries": entries}
    if title:
        data["title"] = title
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def ids_of(path: Path) -> list:
    return [e.get("id") for e in read(path).get("entries", [])]


def cat_of(path: Path, cid: str):
    return next((e.get("category") for e in read(path).get("entries", [])
                 if e.get("id") == cid), None)


async def call(handler, payload, plugin):
    async def fake_body():
        return payload
    old = api._body
    api._body = fake_body
    try:
        return await handler(plugin)
    finally:
        api._body = old


def test_collapse_migration(td: Path):
    pools = td / "pools"
    pools.mkdir(parents=True)
    dump(pools / "game.json", "game", [
        {"id": "old_yh", "name": "旧角色", "series": "鸣潮", "category": "game",
         "tags_en": "1girl", "strategy": {}}], title="游戏卷")
    dump(pools / "anime.json", "anime", [
        {"id": "miku", "name": "初音", "series": "VOCALOID", "category": "anime",
         "tags_en": "1girl", "strategy": {}}], title="动漫卷")
    # 非标准卷：鸣潮查漏补缺（category game）、gal 卷（gal→game）、无类型卷
    dump(pools / "wuthering_waves.json", "wuthering_waves", [
        {"id": "moning", "name": "莫宁", "series": "鸣潮", "category": "game",
         "tags_en": "1girl", "strategy": {}}], title="鸣潮")
    dump(pools / "gal.json", "gal", [
        {"id": "atri", "name": "亚托莉", "series": "ATRI", "category": "gal",
         "tags_en": "1girl", "strategy": {}}], title="galgame卷")
    dump(pools / "demo.json", "demo", [
        {"id": "nomax", "name": "无类型", "series": "某作",
         "tags_en": "1girl", "strategy": {}}], title="演示卷")

    plugin = SimpleNamespace(_data_dir=td, _store=None,
                             _today=lambda s: "2026-11-17")
    res = api.migrate_pack_collapse(plugin)
    check("迁移并入了非标准卷角色", res["merged"] == 3, str(res))
    check("移除了全部非标准卷",
          sorted(res["removed_packs"]) ==
          ["demo", "gal", "wuthering_waves"], str(res))
    for f in ("wuthering_waves.json", "gal.json", "demo.json"):
        check(f"{f} 已离开 pools/", not (pools / f).is_file())
    check("game.json 收下鸣潮卷角色", "moning" in ids_of(pools / "game.json"))
    check("gal 角色并入游戏卷且类型改写",
          "atri" in ids_of(pools / "game.json")
          and cat_of(pools / "game.json", "atri") == "game")
    check("无类型角色落入其他卷",
          "nomax" in ids_of(pools / "other.json")
          and cat_of(pools / "other.json", "nomax") == "other")
    check("动漫卷不受影响", "miku" in ids_of(pools / "anime.json"))
    # 回收站备份
    trash_files = [str(p) for p in (td / "_trash").rglob("wuthering_waves.json")]
    check("原卷文件在回收站可找回", len(trash_files) == 1, str(trash_files))
    # 幂等：第二次运行不再有合并
    res2 = api.migrate_pack_collapse(plugin)
    check("重复迁移幂等", res2["merged"] == 0 and res2["removed_packs"] == [],
          str(res2))

    # 同 id 冲突：保留资产分高的一份（带 cover 的胜出）
    dump(pools / "zstray.json", "zstray", [
        {"id": "old_yh", "name": "旧角色加强版", "series": "鸣潮",
         "category": "game", "tags_en": "1girl", "cover": "refs/x.png",
         "strategy": {}}], title="游散卷")
    api.migrate_pack_collapse(plugin)
    kept = next(e for e in read(pools / "game.json")["entries"]
                if e["id"] == "old_yh")
    check("同 id 冲突保留资产分高的一份", kept.get("cover") == "refs/x.png",
          str(kept))


def test_type_registry(td: Path):
    plugin = SimpleNamespace(_data_dir=td, _store=None,
                             _today=lambda s: "2026-11-17")
    reg = api.load_type_registry(plugin)
    check("默认内置三类型",
          [t["key"] for t in reg] == ["anime", "game", "other"], str(reg))

    res = asyncio.run(call(api.h_category_add, {"name": "小说"}, plugin))
    check("自定义类型添加成功", res["status"] == "ok", str(res))
    reg = api.load_type_registry(plugin)
    check("注册表带上自定义类型",
          any(t["key"] == "小说" and t["title"] == "小说" for t in reg), str(reg))
    res2 = asyncio.run(call(api.h_category_add, {"name": "小说"}, plugin))
    check("重复添加被拒绝", res2["status"] == "error", str(res2))
    res3 = asyncio.run(call(api.h_category_add, {"name": "游戏"}, plugin))
    check("与内置类型重名被拒绝", res3["status"] == "error", str(res3))

    # 改名：注册表 + 卷文件 + packs 行三处联动
    st = store.Store(td / "waifu.db")
    try:
        plugin._store = st
        pools = td / "pools"
        pools.mkdir(parents=True, exist_ok=True)
        dump(pools / "小说.json", "小说", [
            {"id": "b1", "name": "书角色", "series": "某书", "category": "小说",
             "tags_en": "1girl", "strategy": {}}], title="小说")
        st.sync_pack_file(pools / "小说.json")
        res = asyncio.run(call(api.h_category_rename,
                               {"key": "小说", "name": "轻小说"}, plugin))
        check("自定义类型改名成功", res["status"] == "ok", str(res))
        check("卷文件随改名移动",
              (pools / "轻小说.json").is_file() and not (pools / "小说.json").exists())
        check("改名后条目保留",
              "b1" in ids_of(pools / "轻小说.json"))
        prow = st._conn.execute(
            "SELECT name, title FROM packs WHERE name='轻小说'").fetchone()
        check("新表 packs 行原位改名", prow and prow["title"] == "轻小说",
              str(dict(prow) if prow else None))
        cres = st._conn.execute(
            "SELECT p.name FROM characters c JOIN packs p ON p.id=c.pack_id"
            " WHERE c.char_id='b1'").fetchone()
        check("角色仍挂在新命名的卷上", cres and cres["name"] == "轻小说",
              str(dict(cres) if cres else None))
        res2 = asyncio.run(call(api.h_category_rename,
                                {"key": "anime", "name": "动画"}, plugin))
        check("内置类型改名被拒绝", res2["status"] == "error", str(res2))

        # 删除：非空拒绝；空类型可删
        res3 = asyncio.run(call(api.h_category_delete, {"key": "轻小说"}, plugin))
        check("非空类型删除被拒绝", res3["status"] == "error", str(res3))
        dump(pools / "zzz.json", "zzz", [], title="空卷")
        st.sync_pack_file(pools / "zzz.json")
        res4 = asyncio.run(call(api.h_category_add, {"name": "临时"}, plugin))
        check("空类型添加成功", res4["status"] == "ok", str(res4))
        res5 = asyncio.run(call(api.h_category_delete, {"key": "临时"}, plugin))
        check("空类型删除成功", res5["status"] == "ok", str(res5))
        res6 = asyncio.run(call(api.h_category_delete, {"key": "anime"}, plugin))
        check("内置类型删除被拒绝", res6["status"] == "error", str(res6))
    finally:
        st.close()


def test_prune_and_tags(td: Path):
    st = store.Store(td / "waifu.db")
    try:
        pools = td / "pools"
        pools.mkdir(parents=True, exist_ok=True)
        dump(pools / "game.json", "game", [
            {"id": "keep1", "name": "留下的", "series": "鸣潮",
             "category": "game", "tags_en": "1girl", "strategy": {}}],
            title="游戏卷")
        dump(pools / "stale.json", "stale", [
            {"id": "gone1", "name": "该消失的", "series": "鸣潮",
             "category": "game", "tags_en": "1girl", "strategy": {}}],
            title="旧卷")
        st.sync_pack_file(pools / "game.json")
        st.sync_pack_file(pools / "stale.json")
        st.sync_pack_file(pools / "game.json")   # 收敛后 game 重复同步重建角色
        res = st.prune_packs({"game"})
        check("prune_packs 移除 stale 卷", "stale" in res["removed"], str(res))
        check("stale 卷角色行已清（随后续 sync 重建到新卷）",
              st._conn.execute("SELECT 1 FROM characters WHERE char_id='gone1'")
              .fetchone() is None)
        check("keep 卷角色不受影响",
              st._conn.execute("SELECT 1 FROM characters WHERE char_id='keep1'")
              .fetchone() is not None)

        # 标签过滤：命中停用标签的角色不入候选
        packs = [
            {"pack": "game", "enabled": True, "entries": [
                {"id": "a", "name": "甲", "series": "鸣潮", "category": "game",
                 "tags_en": "1girl", "verified": True, "strategy": {},
                 "user_tags": ["测试"]},
                {"id": "b", "name": "乙", "series": "鸣潮", "category": "game",
                 "tags_en": "1girl", "verified": True, "strategy": {},
                 "user_tags": []},
                {"id": "c", "name": "丙", "series": "鸣潮", "category": "game",
                 "tags_en": "1girl", "verified": True, "strategy": {},
                 "user_tags": "未完工, 草稿"}]},
        ]
        cands = pool.candidates_for(packs, "", "", True,
                                    disabled_tags={"测试", "未完工"})
        check("停用标签过滤命中角色",
              [c["id"] for c in cands] == ["b"], str([c["id"] for c in cands]))
        cands_all = pool.candidates_for(packs, "", "", True)
        check("未停用标签时三人都在", len(cands_all) == 3, str(len(cands_all)))
    finally:
        st.close()


def main():
    jobs = [("collapse_migration", test_collapse_migration),
            ("type_registry", test_type_registry),
            ("prune_and_tags", test_prune_and_tags)]
    for name, fn in jobs:
        with tempfile.TemporaryDirectory() as t:
            print(f"--- {name} ---")
            fn(Path(t))
    if FAILURES:
        print(f"test_pack_collapse: {len(FAILURES)} FAILED")
        sys.exit(1)
    print("test_pack_collapse: ALL OK")


if __name__ == "__main__":
    main()
