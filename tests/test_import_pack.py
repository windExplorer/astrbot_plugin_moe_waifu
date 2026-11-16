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
    pool.set_packs_provider(st.load_packs)
    plugin = type("P", (), {"_data_dir": td, "_store": st,
                            "_today": lambda s: "2026-11-17"})()


def test_cross_pack_move(td: Path):
    setup_env(td)
    try:
        # 旧条目带全量资产字段（参考图绑定 / 封面 / 资料 / 头像 / 入库日期）——
        # v0.67.0 移动把它们全丢过，这里就是回归现场
        dump(pools / "wuthering_waves.json", "wuthering_waves", [
            {"id": "moning", "name": "莫宁", "series": "鸣潮", "category": "game",
             "tags_en": "1girl", "verified": True,
             "strategy": {"type": "img2img", "ref": "refs/a.png",
                          "refs": ["refs/a.png", "refs/b.png"],
                          "draw_ref": "refs/b.png"},
             "cover": "refs/b.png",
             "profile": {"简介": "测试资料", "生日": "7月23日"},
             "avatar": "avatars/moning.png",
             "added_at": "2026-11-01"}], title="鸣潮")
        # 现实中条目是经应用写入的（_json_save 钩子已同步新表）——这里同样
        # 先同步，owner 扫描拿到的才是带资产的完整条目
        st.sync_pack_file(pools / "wuthering_waves.json")
        res = asyncio.run(call(api.h_save_entry, {
            "pack": "game",
            # 表单重建条目：只有画法 / 主参考 / 名字等托管字段，不带 refs /
            # cover / profile / avatar（编辑抽屉的真实 payload 形状）
            "entry": {"id": "moning", "name": "莫宁", "series": "鸣潮",
                      "category": "game", "tags_en": "1girl",
                      "strategy": {"type": "img2img", "ref": "refs/a.png"},
                      "verified": True}}))
        check("改卷保存成功（不再报 id 重复）", res["status"] == "ok", str(res))
        check("返回移动来源卷", res["data"].get("moved_from") == "wuthering_waves",
              str(res.get("data")))
        check("目标卷文件已含角色", "moning" in ids_of(pools / "game.json"))
        check("原卷文件已摘除角色", "moning" not in ids_of(pools / "wuthering_waves.json"))
        moved = next(e for e in read(pools / "game.json")["entries"]
                     if e["id"] == "moning")
        check("移动保留参考图绑定",
              moved["strategy"].get("refs") == ["refs/a.png", "refs/b.png"],
              str(moved["strategy"]))
        check("移动保留出卡源", moved["strategy"].get("draw_ref") == "refs/b.png",
              str(moved["strategy"]))
        check("移动保留封面", moved.get("cover") == "refs/b.png", str(moved))
        check("移动保留资料", moved.get("profile", {}).get("生日") == "7月23日",
              str(moved.get("profile")))
        check("移动保留头像指针", moved.get("avatar") == "avatars/moning.png",
              str(moved.get("avatar")))
        check("移动保留入库日期", moved.get("added_at") == "2026-11-01",
              str(moved.get("added_at")))
        check("表单字段正常更新", moved["name"] == "莫宁"
              and moved["strategy"].get("type") == "img2img", str(moved))
        prow = st._conn.execute(
            "SELECT p.name FROM characters c JOIN packs p ON p.id=c.pack_id"
            " WHERE c.char_id='moning'").fetchone()
        check("新表行随同步落到目标卷", prow and prow["name"] == "game", str(prow))

        # 同卷编辑：原位替换、不产生移动
        res2 = asyncio.run(call(api.h_save_entry, {
            "pack": "game",
            "entry": {"id": "moning", "name": "莫宁改", "series": "鸣潮",
                      "category": "game", "tags_en": "1girl", "strategy": {}}}))
        check("同卷编辑 replaced", res2["data"].get("replaced") is True, str(res2))
        check("同卷编辑无移动", res2["data"].get("moved_from") == "", str(res2))
        moved2 = next(e for e in read(pools / "game.json")["entries"]
                      if e["id"] == "moning")
        check("同卷编辑不丢资产（头像 / 参考图仍在）",
              moved2.get("avatar") == "avatars/moning.png"
              and moved2["strategy"].get("refs") == ["refs/a.png", "refs/b.png"],
              str(moved2))
        check("角色只剩一份（原卷没被复制回去）",
              "moning" not in ids_of(pools / "wuthering_waves.json"))
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
        check("条目指针已恢复",
              next(e for e in read(pools / "wuthering_waves.json")["entries"]
                   if e["id"] == "moning")["avatar"] == "avatars/moning.png")
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
        # 未选卷（pack 留空）、JSON 是数组没有 pack → 并入其他卷；空 series → 其他
        res = asyncio.run(call(api.h_import_pack, dict(payload)))
        check("无 pack 导入成功", res["status"] == "ok", str(res))
        check("落到其他卷", res["data"]["pack"] == "other", str(res["data"]))
        other = pools / "other.json"
        check("其他卷文件已创建且标题正确",
              other.is_file() and read(other).get("title") == "其他卷",
              str(read(other))[:120])
        check("空 series 并入「其他」作品",
              next(e for e in read(other)["entries"]
                   if e["id"] == "a2")["series"] == "其他")
        check("自带 series 保留",
              next(e for e in read(other)["entries"]
                   if e["id"] == "a1")["series"] == "某作")

        # 类型选择：category=anime 覆盖条目自带 game
        res2 = asyncio.run(call(api.h_import_pack, dict(
            payload, category="anime", overwrite=True)))
        check("类型覆盖生效",
              next(e for e in read(other)["entries"]
                   if e["id"] == "a1")["category"] == "anime", str(res2))
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
        # 指定现有卷：并入该卷文件，不新建卷
        res = asyncio.run(call(api.h_import_pack, dict(payload, pack="game")))
        check("指定现有卷导入成功", res["data"]["pack"] == "game", str(res))
        check("角色并入所选卷文件", "new8_1" in ids_of(pools / "game.json"))

        # 跟随 JSON 自带卷（pack 留空 → 用 JSON 的 wuthering_waves）
        res2 = asyncio.run(call(api.h_import_pack, {
            "text": json.dumps({"pack": "wuthering_waves", "title": "鸣潮", "entries": [
                {"id": "new8_2", "name": "新角色2", "series": "鸣潮",
                 "category": "game", "tags_en": "1girl", "strategy": {}}]},
                ensure_ascii=False)}))
        check("整卷对象 pack 为空时跟随 JSON 自带卷",
              res2["data"]["pack"] == "wuthering_waves", str(res2))
        check("自带卷文件追加成功", "new8_2" in ids_of(pools / "wuthering_waves.json"))

        # pack 留空 + 数组 JSON（没有 pack 字段）→ 默认其他卷
        res3 = asyncio.run(call(api.h_import_pack, {
            "text": json.dumps([
                {"id": "new8_3", "name": "新角色3", "series": "鸣潮",
                 "category": "game", "tags_en": "1girl", "strategy": {}}],
                ensure_ascii=False)}))
        check("pack 留空默认其他卷", res3["data"]["pack"] == "other", str(res3))
        check("其他卷追加成功", "new8_3" in ids_of(pools / "other.json"))
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
