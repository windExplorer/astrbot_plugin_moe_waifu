# -*- coding: utf-8 -*-
"""立绘 ↔ 参考图互转的真文件 / 真 SQLite 守卫（可直接运行，退出码即结论）。

复现用户报的两件事：
  ① 旧版「加入参考图库」只把立绘路径 append 进 strategy.refs → 两个列表都有；
  ② 删立绘时把两个列表共同指向的同一个文件 unlink → 参考图也变死链。
修复后要求：转移是「移动」，两列表互斥、文件独立；历史脏数据在删除时能自动保活。
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import draw  # noqa: E402
import pool  # noqa: E402
import store  # noqa: E402
import webui_api as api  # noqa: E402

TODAY = "2026-11-12"
FAILURES = []


def check(name, cond, detail=""):
    if not cond:
        FAILURES.append(name)
        print(f"FAIL: {name} {detail}")
    else:
        print(f"ok: {name}")


def make_plugin(root: Path):
    pools = root / "pools"
    pools.mkdir(parents=True, exist_ok=True)

    def entry(cid, name):
        return {"id": cid, "name": name, "series": "测试作品",
                "category": "anime", "tier": "S", "tags_en": "x, test",
                "color": "#39C5BB", "verified": True,
                "strategy_type": "img2img",
                "strategy": {"ref": "", "refs": []}, "cover": ""}

    write_pack(root, {"pack": "anime", "title": "动漫卷", "enabled": True,
                      "entries": [entry("c1", "小明"), entry("c2", "小红")]})
    st = store.Store(root / "waifu.db")
    plugin = SimpleNamespace(
        _data_dir=root, _store=st, _today=lambda: TODAY,
        _cfg=lambda key, default=None: default, config={})
    return plugin, st


def read_pack(root: Path) -> dict:
    return json.loads((root / "pools" / "anime.json").read_text(encoding="utf-8"))


def write_pack(root: Path, data: dict) -> None:
    (root / "pools" / "anime.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def char_of(root: Path, cid: str) -> dict:
    return next(e for e in read_pack(root)["entries"] if e["id"] == cid)


def put_portrait(root: Path, st, rel: str, content: bytes) -> None:
    fp = root / rel
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_bytes(content)
    st.add_portrait("c1", rel, 123, TODAY, 0, "base")


def test_move_to_ref(root: Path):
    plugin, st = make_plugin(root)
    rel = "portraits/c1/a.png"
    put_portrait(root, st, rel, b"AAA")
    res = api._move_portrait_to_ref(plugin, rel, "c1")
    check("转移返回 refs 新路径", res["to"].startswith("refs/mv_"), str(res))
    check("原立绘文件已移走", not (root / rel).is_file())
    check("立绘列表里不再有这张", st.list_portraits("c1") == [])
    check("参考图文件独立存在且内容不变", (root / res["to"]).read_bytes() == b"AAA")
    check("参考图列表恰好新增一条",
          char_of(root, "c1")["strategy"]["refs"] == [res["to"]])
    check("旧立绘路径不再被任何引用", not api._path_is_referenced(plugin, rel))
    check("内容寻址不会堆重复文件",
          (root / res["to"]).name == api._copy_into_refs(plugin, root / res["to"]).split("/")[-1])
    st.close()


def test_move_to_ref_carries_meta(root: Path):
    """v0.20.0：立绘转参考图时把 prompt/theme/seed/尺寸带进 ref_meta。"""
    plugin, st = make_plugin(root)
    rel = "portraits/c1/a.png"
    put_portrait(root, st, rel, b"CCC")
    st._conn.execute(
        "UPDATE portraits SET prompt=?, theme=?, width=?, height=? WHERE file=?",
        ("立绘提示词", "campus", 700, 1200, rel))
    st._conn.commit()
    res = api._move_portrait_to_ref(plugin, rel, "c1")
    meta = st.get_ref_meta([res["to"]]).get(res["to"])
    check("转参考图带走生成信息",
          meta is not None and meta.get("prompt") == "立绘提示词"
          and meta.get("theme") == "campus" and int(meta.get("width") or 0) == 700,
          str(meta))
    check("来源标记为立绘转入", meta is not None and meta.get("source") == "moved")
    st.close()


def test_soft_delete_flow(root: Path):
    """v0.21.0：删除接口=软删除（文件与行保留可溯源）；
    彻底删除仅对停用行开放且必须 confirm=true。"""
    plugin, st = make_plugin(root)
    rel = "portraits/c1/a.png"
    put_portrait(root, st, rel, b"DDD")
    r1 = asyncio.run(call_handler(plugin, api.h_portrait_delete, {"file": rel}))
    check("删除接口=软删除", r1["status"] == "ok" and r1["data"].get("disabled") == rel,
          str(r1))
    check("文件仍在（已抽到的用户可溯源）", (root / rel).is_file())
    check("行仍在且 active=0", st.portrait_by_file(rel) is not None
          and int(st.portrait_by_file(rel)["active"]) == 0)
    check("配额不再计停用行", st.count_portraits("c1") == 0)
    r2 = asyncio.run(call_handler(plugin, api.h_portrait_delete_hard, {"file": rel}))
    check("未确认拒绝彻底删除", r2["status"] == "error", str(r2))
    r3 = asyncio.run(call_handler(plugin, api.h_portrait_delete_hard,
                                  {"file": rel, "confirm": True}))
    check("停用后可彻底删除", r3["status"] == "ok", str(r3))
    check("彻底删除后行没了", st.portrait_by_file(rel) is None)
    check("文件已离开原位（进回收站）", not (root / rel).is_file())
    st.close()


def test_roundtrip(root: Path):
    plugin, st = make_plugin(root)
    rel = "portraits/c1/a.png"
    put_portrait(root, st, rel, b"BBB")
    moved = api._move_portrait_to_ref(plugin, rel, "c1")
    # 顺手把主参考与封面也指到这张参考图，验证转回时不会留死链
    data = read_pack(root)
    c1 = next(e for e in data["entries"] if e["id"] == "c1")
    c1["strategy"]["ref"] = moved["to"]
    c1["cover"] = moved["to"]
    write_pack(root, data)
    back = api._move_ref_to_portrait(plugin, "c1", moved["to"])
    rows = st.list_portraits("c1")
    check("转回后立绘库恰好一张", len(rows) == 1, str(rows))
    check("转回后的立绘文件存在且内容不变",
          (root / back["to"]).is_file() and (root / back["to"]).read_bytes() == b"BBB")
    check("参考图列表已清空", char_of(root, "c1")["strategy"]["refs"] == [])
    check("主参考已清空（不指向搬走的文件）",
          char_of(root, "c1")["strategy"]["ref"] == "")
    check("封面改指新立绘而非死链", char_of(root, "c1")["cover"] == back["to"])
    # v0.12.5：转回立绘不再物理删原图（误操作能从「未绑定图片」找回）
    check("原 refs 文件保留（可找回）",
          (root / moved["to"]).is_file() and back["file_removed"] is False)
    orphans = [i["path"] for i in api._orphan_items(plugin)]
    check("转移后原图出现在未绑定图片里", moved["to"] in orphans, str(orphans))
    check("绑定互斥：立绘有、参考图列表无",
          len(st.list_portraits("c1")) == 1
          and not char_of(root, "c1")["strategy"]["refs"])
    st.close()


def test_delete_legacy_dirty_data(root: Path):
    """老版脏数据：立绘同时以「相对路径 + 绝对路径」两种写法被绑成参考图。"""
    plugin, st = make_plugin(root)
    rel = "portraits/c1/a.png"
    put_portrait(root, st, rel, b"CCC")
    data = read_pack(root)
    c1 = next(e for e in data["entries"] if e["id"] == "c1")
    c1["strategy"]["refs"] = [rel, str(root / rel)]
    c1["strategy"]["ref"] = rel
    c1["cover"] = rel
    write_pack(root, data)
    res = api._delete_portrait_core(plugin, rel)
    check("删除时检测到引用并另存", res["relocated_to"].startswith("refs/mv_"), str(res))
    check("原立绘文件与库行都没了",
          not (root / rel).is_file() and st.list_portraits("c1") == [])
    check("另存的参考图文件独立存活",
          (root / res["relocated_to"]).read_bytes() == b"CCC")
    c1b = char_of(root, "c1")
    check("refs 两种写法合并去重并改指新文件",
          c1b["strategy"]["refs"] == [res["relocated_to"]], str(c1b["strategy"]["refs"]))
    check("主参考与封面都改指新文件",
          c1b["strategy"]["ref"] == res["relocated_to"]
          and c1b["cover"] == res["relocated_to"])
    check("删除后参考图不再是死链", api._path_is_referenced(plugin, res["relocated_to"]))
    st.close()


def test_delete_clean_portrait(root: Path):
    plugin, st = make_plugin(root)
    rel = "portraits/c1/a.png"
    put_portrait(root, st, rel, b"DDD")
    res = api._delete_portrait_core(plugin, rel)
    check("没被引用的立绘直接删、不产生多余 refs 文件",
          res["relocated_to"] == "" and not (root / rel).is_file()
          and not (root / "refs").exists())
    st.close()


def test_shared_ref_not_deleted(root: Path):
    """同一张 refs 被两个角色共用：转回其中一个不能把文件删掉。"""
    plugin, st = make_plugin(root)
    refs_dir = root / "refs"
    refs_dir.mkdir(parents=True, exist_ok=True)
    shared = refs_dir / "shared.png"
    shared.write_bytes(b"SHARED")
    data = read_pack(root)
    for e in data["entries"]:
        e["strategy"]["refs"] = ["refs/shared.png"]
    write_pack(root, data)
    res = api._move_ref_to_portrait(plugin, "c1", "refs/shared.png")
    check("跨角色共用时原文件保留",
          res["file_removed"] is False and shared.is_file(), str(res))
    check("c1 的参考图已解绑、立绘已归档",
          char_of(root, "c1")["strategy"]["refs"] == []
          and len(st.list_portraits("c1")) == 1)
    check("c2 的引用不受影响",
          char_of(root, "c2")["strategy"]["refs"] == ["refs/shared.png"])
    st.close()


def test_validation(root: Path):
    plugin, st = make_plugin(root)
    check("绝对路径与相对路径归一相同",
          api._norm_ref(plugin, str(root / "refs" / "a.png"))
          == api._norm_ref(plugin, "refs/a.png") == "refs/a.png")
    check("URL 原样返回",
          api._norm_ref(plugin, "https://example.com/a.png") == "https://example.com/a.png")
    check("路径穿越被拒", not api._safe_portrait_rel("portraits/c1/../x.png"))
    for bad, label in (("../../etc/passwd", "越界路径"),
                       ("refs/a.png", "非立绘路径")):
        try:
            api._move_portrait_to_ref(plugin, bad, "c1")
            check(label + " 应被拒绝", False)
        except ValueError:
            check(label + " 被拒绝", True)
    try:
        api._move_ref_to_portrait(plugin, "c1", "portraits/c1/a.png")
        check("立绘路径当成参考图应被拒绝", False)
    except ValueError:
        check("立绘路径当成参考图被拒绝", True)
    try:
        api._move_portrait_to_ref(plugin, "portraits/c1/none.png", "ghost")
        check("不存在的角色应被拒绝", False)
    except ValueError:
        check("不存在的角色被拒绝", True)
    st.close()


async def call_handler(plugin, handler, payload):
    """替换 _body 桩后调用 async handler（真文件 / 真 SQLite）。"""
    async def fake_body():
        return payload
    old = api._body
    api._body = fake_body
    try:
        return await handler(plugin)
    finally:
        api._body = old


def test_save_entry_keeps_refs(root: Path):
    """v0.9.10：工坊编辑点保存不能把 strategy.refs / cover 清空。"""
    plugin, st = make_plugin(root)
    data = read_pack(root)
    e1 = data["entries"][0]
    e1["strategy"].update({"type": "img2img", "ref": "refs/a.png",
                           "refs": ["refs/a.png", "refs/b.png"]})
    e1["cover"] = "refs/cover.png"
    write_pack(root, data)
    form = {"id": "c1", "name": "小明改", "series": "测试作品",
            "category": "anime", "tier": "S", "tags_en": "x, test",
            "color": "#39C5BB", "verified": True, "quote": "", "quotes": [],
            "strategy": {"type": "base"}, "strategy_type": "base"}
    res = asyncio.run(call_handler(plugin, api.h_save_entry,
                                   {"pack": "anime", "entry": form}))
    check("编辑保存成功", res["status"] == "ok", str(res))
    saved = char_of(root, "c1")
    check("编辑保存保留参考图绑定",
          saved["strategy"].get("refs") == ["refs/a.png", "refs/b.png"])
    check("编辑保存保留主参考", saved["strategy"].get("ref") == "refs/a.png")
    check("编辑保存保留封面", saved.get("cover") == "refs/cover.png")
    check("编辑保存更新名字", saved["name"] == "小明改")
    check("编辑保存切换画法生效", saved["strategy"]["type"] == "base")
    st.close()


def test_unbind_shared_ref(root: Path):
    """v0.9.10：试画台删参考图不能把其它角色共用的文件一起删掉。"""
    plugin, st = make_plugin(root)
    refs_dir = root / "refs"
    refs_dir.mkdir(parents=True, exist_ok=True)
    shared = refs_dir / "shared.png"
    shared.write_bytes(b"SHARED")
    data = read_pack(root)
    for e in data["entries"]:
        e["strategy"]["refs"] = ["refs/shared.png"]
    write_pack(root, data)
    r1 = asyncio.run(call_handler(plugin, api.h_refs_unbind,
                                  {"id": "c1", "path": "refs/shared.png",
                                   "delete_file": True}))
    check("共用图解绑不删文件",
          r1["data"]["file_deleted"] is False and shared.is_file(), str(r1))
    check("另一角色引用不受影响",
          char_of(root, "c2")["strategy"]["refs"] == ["refs/shared.png"])
    r2 = asyncio.run(call_handler(plugin, api.h_refs_unbind,
                                  {"id": "c2", "path": "refs/shared.png",
                                   "delete_file": True}))
    check("最后一个引用解绑后才删文件",
          r2["data"]["file_deleted"] is True and not shared.exists(), str(r2))
    st.close()


def test_orphans_recovery(root: Path):
    """v0.9.10：丢失绑定的图片能被扫描出来并一键重新绑定。"""
    plugin, st = make_plugin(root)
    refs_dir = root / "refs"
    refs_dir.mkdir(parents=True, exist_ok=True)
    (refs_dir / "lost.png").write_bytes(b"LOST")
    (refs_dir / "bound.png").write_bytes(b"BOUND")
    data = read_pack(root)
    data["entries"][0]["strategy"]["refs"] = ["refs/bound.png"]
    write_pack(root, data)
    res = asyncio.run(api.h_refs_orphans(plugin))
    paths = [i["path"] for i in res["data"]["items"]]
    check("孤儿扫描只列未绑定图片", paths == ["refs/lost.png"], str(paths))
    bound = asyncio.run(call_handler(plugin, api.h_refs_bind,
                                     {"id": "c1", "path": "refs/lost.png"}))
    check("孤儿可重新绑定", bound["status"] == "ok", str(bound))
    res2 = asyncio.run(api.h_refs_orphans(plugin))
    check("重新绑定后不再算孤儿", res2["data"]["items"] == [])
    st.close()


def test_draw_source(root: Path):
    """v0.10.0：出卡图生图源独立字段的设置、取消、解绑清理。"""
    plugin, st = make_plugin(root)
    refs_dir = root / "refs"
    refs_dir.mkdir(parents=True, exist_ok=True)
    (refs_dir / "a.png").write_bytes(b"A")
    data = read_pack(root)
    data["entries"][0]["strategy"]["refs"] = ["refs/a.png"]
    write_pack(root, data)

    r1 = asyncio.run(call_handler(plugin, api.h_refs_set_draw_source,
                                  {"id": "c1", "path": "refs/a.png"}))
    check("可指定出卡源", r1["status"] == "ok"
          and char_of(root, "c1")["strategy"]["draw_ref"] == "refs/a.png", str(r1))
    check("出卡源被识别为可用", draw.draw_source(
        pool.normalize_entry(char_of(root, "c1"), "anime"), root) != "")
    check("孤儿扫描不把出卡源算孤儿",
          all(i["path"] != "refs/a.png"
              for i in asyncio.run(api.h_refs_orphans(plugin))["data"]["items"]))
    r2 = asyncio.run(call_handler(plugin, api.h_refs_unbind,
                                  {"id": "c1", "path": "refs/a.png"}))
    check("解绑成功", r2["status"] == "ok", str(r2))
    check("解绑后出卡源被清空",
          char_of(root, "c1")["strategy"]["draw_ref"] == "")
    r3 = asyncio.run(call_handler(plugin, api.h_refs_set_draw_source,
                                  {"id": "c1", "path": ""}))
    check("可显式取消出卡源", r3["status"] == "ok"
          and r3["data"]["draw_ref"] == "", str(r3))
    st.close()


def test_orphan_delete(root: Path):
    """v0.12.7：未绑定图片可物理删除；仍被引用 / 越界路径一律拒绝。"""
    plugin, st = make_plugin(root)
    refs_dir = root / "refs"
    refs_dir.mkdir(parents=True, exist_ok=True)
    (refs_dir / "lost.png").write_bytes(b"LOST")
    (refs_dir / "bound.png").write_bytes(b"BOUND")
    data = read_pack(root)
    data["entries"][0]["strategy"]["refs"] = ["refs/bound.png"]
    write_pack(root, data)

    r1 = asyncio.run(call_handler(plugin, api.h_refs_orphan_delete,
                                  {"path": "refs/bound.png"}))
    check("被引用的图拒绝物理删除",
          r1["status"] == "error" and (refs_dir / "bound.png").is_file(), str(r1))
    r2 = asyncio.run(call_handler(plugin, api.h_refs_orphan_delete,
                                  {"path": "refs/lost.png"}))
    check("未绑定图可物理删除",
          r2["status"] == "ok" and not (refs_dir / "lost.png").exists(), str(r2))
    check("删除后不再出现在孤儿列表",
          all(i["path"] != "refs/lost.png"
              for i in asyncio.run(api.h_refs_orphans(plugin))["data"]["items"]))
    r3 = asyncio.run(call_handler(plugin, api.h_refs_orphan_delete,
                                  {"path": "../outside.png"}))
    check("越界路径拒绝删除", r3["status"] == "error", str(r3))
    r4 = asyncio.run(call_handler(plugin, api.h_refs_orphan_delete,
                                  {"path": "portraits/c1/a.png"}))
    check("非 refs/ 路径拒绝删除", r4["status"] == "error", str(r4))
    st.close()


def main():
    jobs = [("move_to_ref", test_move_to_ref),
            ("move_to_ref_meta", test_move_to_ref_carries_meta),
            ("soft_delete_flow", test_soft_delete_flow),
            ("roundtrip", test_roundtrip),
            ("delete_legacy", test_delete_legacy_dirty_data),
            ("delete_clean", test_delete_clean_portrait),
            ("shared_ref", test_shared_ref_not_deleted),
            ("validation", test_validation),
            ("save_keeps_refs", test_save_entry_keeps_refs),
            ("unbind_shared", test_unbind_shared_ref),
            ("orphans", test_orphans_recovery),
            ("draw_source", test_draw_source),
            ("orphan_delete", test_orphan_delete)]
    for name, fn in jobs:
        with tempfile.TemporaryDirectory() as td:
            print(f"--- {name} ---")
            fn(Path(td))
    if FAILURES:
        print(f"test_refs_move: {len(FAILURES)} FAILED")
        sys.exit(1)
    print("test_refs_move: ALL OK")


if __name__ == "__main__":
    main()
