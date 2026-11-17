# -*- coding: utf-8 -*-
"""导入选类型 / 选卷 + 跨卷移动守卫（v0.67.0，可直接运行，退出码即结论）。

用户口径：
  ①导入角色应可选类型（动漫 / 游戏）；
  ②未选卷并入「其他卷」，未填作品并入「其他」作品；
  ③改卷报「id 与卷 X 中的角色重复」但目标卷搜不到 —— 根因 = 查重扫了角色
    自己在原卷的行（char_id 全库唯一）。改卷保存应是「移动」。
守卫点：
  ①h_save_entry 跨卷移动：目标卷新增 + 原卷摘除 + 新表行随同步落到目标卷；
  ②同卷编辑仍是原位替换（moved_from 为空）；
  ③导入默认落「其他卷」（key=other，标题其他卷）、空 series 并入「其他」、
    category 参数生效、pack="" 跟随 JSON 自带卷、指定现有卷正常并入。
"""
import asyncio
import json
import logging
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pool  # noqa: E402
import store  # noqa: E402
import webui_api as api  # noqa: E402

FAILURES = []
logger = logging.getLogger("t")


def check(name, cond, detail=""):
    if not cond:
        FAILURES.append(name)
        print(f"FAIL: {name} {detail}")
    else:
        print(f"ok: {name}")


def dump(path: Path, pack: str, entries: list, title: str = "") -> None:
    data = {"pack": pack, "enabled": True, "entries": entries}
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


async def call(handler, payload):
    async def fake_body():
        return payload
    old = api._body
    api._body = fake_body
    try:
        return await handler(plugin)
    finally:
        api._body = old


def setup_env(td: Path):
    global plugin, st, pools
    pools = td / "pools"
    pools.mkdir(parents=True, exist_ok=True)
    dump(pools / "game.json", "game", [
        {"id": "old_yh", "name": "旧角色", "series": "鸣潮",
         "category": "game", "tags_en": "1girl", "strategy": {}}],
        title="游戏卷")
    dump(pools / "wuthering_waves.json", "wuthering_waves", [
        {"id": "moning", "name": "莫宁", "series": "鸣潮",
         "category": "game", "tags_en": "1girl", "strategy": {}}],
        title="鸣潮")
    st = store.Store(td / "waifu.db")
    st.migrate_relational(pools, td, logger=logger)
    api.set_pack_sync(st.sync_pack_file)
    api.set_pack_save(st.save_pack_data)   # v0.69.0 DB 真源：写入也挂上
    pool.set_packs_provider(st.load_packs)
    plugin = type("P", (), {"_data_dir": td, "_store": st,
                            "_today": lambda s: "2026-11-17"})()


def test_cross_pack_move(td: Path):
    setup_env(td)
    try:
        # v0.69.0 DB 真源 + 卷维度删：角色只按 category 存。用 setup 里没有的
        # 新 id（atri）做跨类型移动：先在 other，存到 game 即移动。旧条目带全量
        # 资产字段——v0.67.0 移动把它们丢过，这里是回归现场。
        st.save_entry({"id": "atri", "name": "亚托莉", "series": "ATRI",
                       "category": "other", "tags_en": "1girl", "verified": True,
                       "strategy": {"type": "img2img", "ref": "refs/a.png",
                                    "refs": ["refs/a.png", "refs/b.png"],
                                    "draw_ref": "refs/b.png"},
                       "cover": "refs/b.png",
                       "profile": {"简介": "测试资料", "生日": "7月23日"},
                       "avatar": "avatars/atri.png",
                       "added_at": "2026-11-01"}, "other")
        check("初始角色在 other 类型", st.find_entry_category("atri") == "other")
        res = asyncio.run(call(api.h_save_entry, {
            "pack": "game",
            # 表单重建条目：只有托管字段，不带 refs / cover / profile / avatar
            "entry": {"id": "atri", "name": "亚托莉", "series": "ATRI",
                      "category": "game", "tags_en": "1girl",
                      "strategy": {"type": "img2img", "ref": "refs/a.png"},
                      "verified": True}}))
        check("改类型保存成功（不再报 id 重复）", res["status"] == "ok", str(res))
        check("返回移动来源类型", res["data"].get("moved_from") == "other",
              str(res.get("data")))
        check("角色已挪到 game 类型", st.find_entry_category("atri") == "game")
        moved = st.find_entry("atri")
        check("移动保留参考图绑定",
              moved["strategy"].get("refs") == ["refs/a.png", "refs/b.png"],
              str(moved["strategy"]))
        check("移动保留出卡源", moved["strategy"].get("draw_ref") == "refs/b.png",
              str(moved["strategy"]))
        check("移动保留封面", moved.get("cover") == "refs/b.png", str(moved))
        check("移动保留资料", moved.get("profile", {}).get("生日") == "7月23日",
              str(moved.get("profile")))
        check("移动保留头像指针", moved.get("avatar") == "avatars/atri.png",
              str(moved.get("avatar")))
        check("移动保留入库日期", moved.get("added_at") == "2026-11-01",
              str(moved.get("added_at")))
        check("表单字段正常更新", moved["name"] == "亚托莉"
              and moved["strategy"].get("type") == "img2img", str(moved))
        check("角色全库只有一份（category=game）",
              st._conn.execute("SELECT COUNT(*) AS n FROM characters WHERE char_id='atri'"
                               ).fetchone()["n"] == 1)

        # 同类型编辑：原位替换、不产生移动
        res2 = asyncio.run(call(api.h_save_entry, {
            "pack": "game",
            "entry": {"id": "atri", "name": "亚托莉改", "series": "ATRI",
                      "category": "game", "tags_en": "1girl", "strategy": {}}}))
        check("同类型编辑 replaced", res2["data"].get("replaced") is True, str(res2))
        check("同类型编辑无移动", res2["data"].get("moved_from") == "", str(res2))
        moved2 = st.find_entry("atri")
        check("同类型编辑不丢资产（头像 / 参考图仍在）",
              moved2.get("avatar") == "avatars/atri.png"
              and moved2["strategy"].get("refs") == ["refs/a.png", "refs/b.png"],
              str(moved2))
        check("旧 other 类型里已无该角色",
              st.find_entry_category("atri") == "game")
    finally:
        st.close()


def test_avatar_heal(td: Path):
    setup_env(td)
    try:
        av_dir = td / "avatars"
        av_dir.mkdir(parents=True, exist_ok=True)
        (av_dir / "moning.png").write_bytes(b"PNG")
        # 条目 avatar 指针为空（v0.67.0 移动丢指针的现场）→ 自愈补回
        dump(pools / "wuthering_waves.json", "wuthering_waves", [
            {"id": "moning", "name": "莫宁", "series": "鸣潮", "category": "game",
             "tags_en": "1girl", "strategy": {}, "avatar": ""}], title="鸣潮")
        st.sync_pack_file(pools / "wuthering_waves.json")
        res = api.heal_avatar_pointers(plugin)
        check("自愈补回 1 个指针", res["healed"] == 1, str(res))
        check("条目指针已恢复（DB 真源）",
              st.find_entry("moning")["avatar"] == "avatars/moning.png")
        # 已有指针的条目不动
        res2 = api.heal_avatar_pointers(plugin)
        check("幂等（第二次 0 个）", res2["healed"] == 0, str(res2))
    finally:
        st.close()


def test_import_defaults(td: Path):
    setup_env(td)
    try:
        payload = {
            "text": json.dumps([
                {"id": "a1", "name": "角色A", "tags_en": "1girl", "series": "某作",
                 "category": "game", "strategy": {}},
                {"id": "a2", "name": "角色B", "tags_en": "1girl", "strategy": {}},
            ], ensure_ascii=False)}
        # v0.68.0：无目标卷——类型路由：a1 自带 game → 游戏卷；a2 无类型 → 其他卷
        res = asyncio.run(call(api.h_import_pack, dict(payload)))
        check("无类型参数导入成功", res["status"] == "ok", str(res))
        check("返回落卷类型标题", res["data"]["pack"] == "游戏、其他",
              str(res["data"]))
        # v0.69.0 DB 真源：导入直接落 DB，断言读 DB
        check("自带类型条目路由到对应类型", st.find_entry_category("a1") == "game")
        check("无类型条目落入其他类型", st.find_entry_category("a2") == "other")
        check("空 series 并入「其他」作品",
              st.find_entry("a2")["series"] == "其他")
        check("自带 series 保留", st.find_entry("a1")["series"] == "某作")

        # 类型选择：category=anime 指定类型；a1 已存在 game 卷 → 跨类型跳过
        res2 = asyncio.run(call(api.h_import_pack, dict(
            payload, category="anime", overwrite=True)))
        check("跨类型已存在跳过并提示改类型",
              any("改类型请用编辑抽屉" in sk.get("reason", "")
                  for sk in res2["data"]["skipped"]), str(res2["data"]))
        check("结果带回类型", res2["data"].get("category") == "anime", str(res2))
    finally:
        st.close()


def test_import_pack_selection(td: Path):
    setup_env(td)
    try:
        payload = {
            "text": json.dumps({"pack": "wuthering_waves", "title": "鸣潮", "entries": [
                {"id": "new8_1", "name": "新角色", "series": "鸣潮",
                 "category": "game", "tags_en": "1girl", "strategy": {}}]},
                ensure_ascii=False)}
        # v0.68.0：指定类型 = 落该类型卷；JSON 自带 pack 字段不再生效
        res = asyncio.run(call(api.h_import_pack, dict(payload, category="game")))
        check("指定类型导入成功", res["data"]["pack"] == "游戏", str(res))
        check("角色并入 game 类型（DB）", st.find_entry_category("new8_1") == "game")

        # JSON 自带 pack 字段不再生效——按条目类型路由，不会创建一次性卷
        res2 = asyncio.run(call(api.h_import_pack, {
            "text": json.dumps({"pack": "wuthering_waves", "title": "鸣潮", "entries": [
                {"id": "new8_2", "name": "新角色2", "series": "鸣潮",
                 "category": "game", "tags_en": "1girl", "strategy": {}}]},
                ensure_ascii=False)}))
        check("JSON 自带 pack 不再生效（按类型落卷）",
              res2["data"]["pack"] == "游戏", str(res2))
        check("角色并入 game 类型（DB）", st.find_entry_category("new8_2") == "game")
        check("不会创建 wuthering_waves 类型",
              st.find_entry_category("new8_2") != "wuthering_waves")

        # 条目自带 category（game）→ 路由到游戏卷；真正无类型的才落 other
        res3 = asyncio.run(call(api.h_import_pack, {
            "text": json.dumps([
                {"id": "new8_3", "name": "新角色3", "series": "鸣潮",
                 "category": "game", "tags_en": "1girl", "strategy": {}}],
                ensure_ascii=False)}))
        check("条目自带类型路由到对应卷", res3["data"]["pack"] == "游戏", str(res3))
        check("角色并入 game 类型（DB）", st.find_entry_category("new8_3") == "game")
    finally:
        st.close()


def main():
    jobs = [("cross_pack_move", test_cross_pack_move),
            ("avatar_heal", test_avatar_heal),
            ("import_defaults", test_import_defaults),
            ("import_pack_selection", test_import_pack_selection)]
    for name, fn in jobs:
        with tempfile.TemporaryDirectory() as t:
            print(f"--- {name} ---")
            fn(Path(t) / "data")
    if FAILURES:
        print(f"test_import_pack: {len(FAILURES)} FAILED")
        sys.exit(1)
    print("test_import_pack: ALL OK")


if __name__ == "__main__":
    main()
