# -*- coding: utf-8 -*-
"""参考图停用 / 启用 / 彻底删除守卫（v0.65.0，可直接运行，退出码即结论）。

用户口径：角色详情里参考图的「删除」要跟立绘一样是软删除——
  删除 = 停用（strategy.refs → refs_disabled，文件与绑定保留，可恢复）；
  停用列表里才提供「启用」与「彻底删除」（摘绑定 + 文件进回收站）。
守卫点：
  ① 停用 / 启用是移动语义，两边互斥；
  ② 停用图的主参考 / 出卡源一并清空（不留死链）；
  ③ 彻底删除只对已停用开放、必须 confirm、文件进回收站；
  ④ 共用图只摘本角色的绑定、文件保留（v0.9.10 同款守卫）；
  ⑤ 停用图不算孤儿、`refs_disabled` 算引用（不会被孤儿清理误删）；
  ⑥ 编辑表单保存（merge）不清空 refs_disabled；
  ⑦ refs/list 的 items 只含启用中的，停用清单走 items_disabled。
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
import webui_api as api  # noqa: E402

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
    plugin = SimpleNamespace(
        _data_dir=root, _store=None, _today=lambda: "2026-11-17",
        _cfg=lambda key, default=None: default, config={})
    return plugin


def read_pack(root: Path) -> dict:
    return json.loads((root / "pools" / "anime.json").read_text(encoding="utf-8"))


def write_pack(root: Path, data: dict) -> None:
    (root / "pools" / "anime.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def char_of(root: Path, cid: str) -> dict:
    return next(e for e in read_pack(root)["entries"] if e["id"] == cid)


def char_in(data: dict, cid: str) -> dict:
    """data 本地条目（可原地改后随 write_pack 落盘）。"""
    return next(e for e in data["entries"] if e["id"] == cid)


def put_ref(root: Path, rel: str, content: bytes = b"IMG") -> None:
    fp = root / rel
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_bytes(content)


async def call_handler(plugin, handler, payload):
    async def fake_body():
        return payload
    old = api._body
    api._body = fake_body
    try:
        return await handler(plugin)
    finally:
        api._body = old


def test_disable_enable(root: Path):
    plugin = make_plugin(root)
    put_ref(root, "refs/a.png", b"A")
    data = read_pack(root)
    char_in(data, "c1")["strategy"].update({
        "refs": ["refs/a.png"], "ref": "refs/a.png", "draw_ref": "refs/a.png"})
    write_pack(root, data)

    res = api._disable_ref_core(plugin, "c1", "refs/a.png")
    st = char_of(root, "c1")["strategy"]
    check("停用后 refs 不再包含这张", st["refs"] == [], str(st))
    check("停用后进 refs_disabled", st["refs_disabled"] == ["refs/a.png"], str(st))
    check("主参考指向停用图时一并清空", st["ref"] == "", str(st))
    check("出卡源指向停用图时一并清空", st["draw_ref"] == "", str(st))
    check("文件仍在（软删除不碰文件）", (root / "refs/a.png").is_file())

    try:
        api._disable_ref_core(plugin, "c1", "refs/a.png")
        check("重复停用应被拒绝", False)
    except ValueError:
        check("重复停用被拒绝（图已不在 refs）", True)

    api._enable_ref_core(plugin, "c1", "refs/a.png")
    st2 = char_of(root, "c1")["strategy"]
    check("启用后回到 refs", st2["refs"] == ["refs/a.png"], str(st2))
    check("启用后 refs_disabled 清空", st2["refs_disabled"] == [], str(st2))
    check("启用不自动还原主参考", st2["ref"] == "", str(st2))
    try:
        api._enable_ref_core(plugin, "c1", "refs/a.png")
        check("重复启用应被拒绝", False)
    except ValueError:
        check("重复启用被拒绝（图不在停用列表）", True)


def test_hard_delete(root: Path):
    plugin = make_plugin(root)
    put_ref(root, "refs/a.png", b"A")
    data = read_pack(root)
    char_in(data, "c1")["strategy"]["refs"] = ["refs/a.png"]
    write_pack(root, data)

    try:
        api._delete_ref_hard_core(plugin, "c1", "refs/a.png")
        check("未停用直接彻底删除应被拒绝", False)
    except ValueError:
        check("未停用的图拒绝彻底删除", True)

    api._disable_ref_core(plugin, "c1", "refs/a.png")
    res = api._delete_ref_hard_core(plugin, "c1", "refs/a.png")
    check("彻底删除摘掉停用绑定",
          char_of(root, "c1")["strategy"].get("refs_disabled") == [], str(res))
    check("文件已离开 refs/（进回收站）", not (root / "refs/a.png").is_file())
    trash_pics = [p for p in (root / "_trash").rglob("refs/a.png")]
    check("文件可在回收站找到", len(trash_pics) == 1, str(trash_pics))
    check("回收站 meta 记录归属角色", trash_pics and json.loads(
        (trash_pics[0].parent.parent / "_meta.json").read_text(encoding="utf-8")
    )["files"]["refs/a.png"].get("char_id") == "c1")

    r = asyncio.run(call_handler(plugin, api.h_refs_delete_hard,
                                 {"id": "c1", "path": "refs/a.png"}))
    check("handler 无 confirm 拒绝彻底删除", r["status"] == "error", str(r))


def test_hard_delete_shared(root: Path):
    """共用图（v0.9.10 同款）：只摘本角色绑定，文件留给还在用的角色。"""
    plugin = make_plugin(root)
    put_ref(root, "refs/shared.png", b"SHARED")
    data = read_pack(root)
    for e in data["entries"]:
        e["strategy"]["refs_disabled"] = ["refs/shared.png"]
    write_pack(root, data)

    res1 = api._delete_ref_hard_core(plugin, "c1", "refs/shared.png")
    check("c1 绑定已摘", char_of(root, "c1")["strategy"]["refs_disabled"] == [])
    check("c2 绑定不受影响",
          char_of(root, "c2")["strategy"]["refs_disabled"] == ["refs/shared.png"])
    check("仍被 c2 引用：文件保留且带说明",
          res1["file_deleted"] is False and (root / "refs/shared.png").is_file()
          and "引用" in res1["file_skipped"], str(res1))
    res2 = api._delete_ref_hard_core(plugin, "c2", "refs/shared.png")
    check("最后一个引用删除后文件进回收站",
          res2["file_deleted"] is True and not (root / "refs/shared.png").is_file(),
          str(res2))


def test_disabled_not_orphan(root: Path):
    """停用图不算孤儿；refs_disabled 算引用（孤儿删除 / 解绑删除都会拒绝）。"""
    plugin = make_plugin(root)
    put_ref(root, "refs/bound.png", b"B")
    put_ref(root, "refs/lost.png", b"L")
    data = read_pack(root)
    char_in(data, "c1")["strategy"]["refs_disabled"] = ["refs/bound.png"]
    write_pack(root, data)

    orphans = [i["path"] for i in api._orphan_items(plugin)]
    check("停用图不出现在未绑定图片里", "refs/bound.png" not in orphans, str(orphans))
    check("真正无绑定的图仍是孤儿", "refs/lost.png" in orphans, str(orphans))
    check("refs_disabled 算引用",
          api._path_is_referenced(plugin, "refs/bound.png") is True)

    r = asyncio.run(call_handler(plugin, api.h_refs_orphan_delete,
                                 {"path": "refs/bound.png"}))
    check("孤儿删除拒绝仍被停用绑定持有的图",
          r["status"] == "error" and (root / "refs/bound.png").is_file(), str(r))


def test_save_entry_keeps_disabled(root: Path):
    """编辑表单保存（merge）不清空 refs_disabled——与 refs 同款守卫。"""
    plugin = make_plugin(root)
    data = read_pack(root)
    char_in(data, "c1")["strategy"].update({
        "type": "img2img", "ref": "refs/a.png", "refs": ["refs/a.png"],
        "refs_disabled": ["refs/d.png", "refs/e.png"]})
    write_pack(root, data)
    form = {"id": "c1", "name": "小明改", "series": "测试作品",
            "category": "anime", "tier": "S", "tags_en": "x, test",
            "color": "#39C5BB", "verified": True, "quote": "", "quotes": [],
            "strategy": {"type": "base"}, "strategy_type": "base"}
    res = asyncio.run(call_handler(plugin, api.h_save_entry,
                                   {"pack": "anime", "entry": form}))
    st = char_of(root, "c1")["strategy"]
    check("编辑保存成功", res["status"] == "ok", str(res))
    check("编辑保存保留停用清单",
          st.get("refs_disabled") == ["refs/d.png", "refs/e.png"], str(st))
    check("编辑保存保留启用清单", st.get("refs") == ["refs/a.png"], str(st))


def test_refs_list_shape(root: Path):
    """refs/list：items 只含启用中的；停用清单走 items_disabled（选图面自动不见停用图）。"""
    plugin = make_plugin(root)
    put_ref(root, "refs/a.png", b"A")
    put_ref(root, "refs/d.png", b"D")
    data = read_pack(root)
    char_in(data, "c1")["strategy"].update({
        "ref": "refs/a.png", "refs": ["refs/a.png"],
        "refs_disabled": ["refs/d.png"]})
    write_pack(root, data)
    res = asyncio.run(call_handler(plugin, api.h_refs_list, {"id": "c1"}))
    check("refs/list 返回成功", res["status"] == "ok", str(res))
    check("items 只含启用中的图",
          [i["path"] for i in res["data"]["items"]] == ["refs/a.png"], str(res["data"]))
    check("items_disabled 只含停用图",
          [i["path"] for i in res["data"]["items_disabled"]] == ["refs/d.png"],
          str(res["data"]))


def main():
    jobs = [("disable_enable", test_disable_enable),
            ("hard_delete", test_hard_delete),
            ("hard_delete_shared", test_hard_delete_shared),
            ("disabled_not_orphan", test_disabled_not_orphan),
            ("save_keeps_disabled", test_save_entry_keeps_disabled),
            ("refs_list_shape", test_refs_list_shape)]
    for name, fn in jobs:
        with tempfile.TemporaryDirectory() as td:
            print(f"--- {name} ---")
            fn(Path(td))
    if FAILURES:
        print(f"test_refs_soft_delete: {len(FAILURES)} FAILED")
        sys.exit(1)
    print("test_refs_soft_delete: ALL OK")


if __name__ == "__main__":
    main()
