# -*- coding: utf-8 -*-
"""导入覆盖 / 重复角色去重 自检（可直接运行，退出码即结论；不联网、不依赖 astrbot）。

守卫点：导入覆盖只改基础字段（参考图/封面/验证状态不丢）、资产评分与合并、
并查集重复扫描（同 id / 同名同作品）、立绘归属迁移不删行。
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pool  # noqa: E402
import store  # noqa: E402


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL: {name} {detail}")
        sys.exit(1)
    print(f"ok: {name}")


def E(cid, name, series="某作", **kw):
    d = {"id": cid, "name": name, "tags_en": "1girl", "series": series,
         "verified": False, "strategy": {}, "cover": ""}
    d.update(kw)
    return d


# ---------- 1) 导入覆盖：只改基础字段 ----------
old = E("a", "旧名", "旧作", verified=True, cover="cover.png",
        strategy={"type": "img2img", "ref": "refs/main.png",
                  "refs": ["refs/1.png"], "denoise": 0.4})
new = pool.normalize_entry(
    E("a", "新名", "新作", verified=False, strategy={"type": "base"}), "pack")
merged = pool.merge_imported_entry(old, new)
check("覆盖更新名字/作品/标签", merged["name"] == "新名"
      and merged["series"] == "新作")
check("覆盖保留参考图绑定原封不动", merged["strategy"] == old["strategy"])
check("覆盖保留封面与验证状态",
      merged["cover"] == "cover.png" and merged["verified"] is True)
check("覆盖不改 id / pack", merged["id"] == "a" and merged.get("pack") is None)
check("覆盖是纯函数（不改原条目）", old["name"] == "旧名")

# ---------- 2) 资产评分 / 合并 ----------
rich = E("b", "B", strategy={"refs": ["r1.png", "r2.png"], "ref": "r0.png"},
         cover="c.png", verified=True)
poor = E("c", "C")
check("资产分：富 > 贫", pool.asset_score(rich, 3) > pool.asset_score(poor, 0))
check("立绘权重最高（1 张立绘赢过一堆参考图）",
      pool.asset_score(poor, 1) > pool.asset_score(rich, 0))
check("参考图路径去重计数", len(pool.ref_paths(rich)) == 3)
joined = pool.merge_entry_assets(poor, rich)
check("合并参考图不丢（refs / ref）",
      set(joined["strategy"]["refs"]) == {"r1.png", "r2.png"}
      and joined["strategy"]["ref"] == "r0.png")
check("合并封面与验证状态",
      joined["cover"] == "c.png" and joined["verified"] is True)
check("合并是纯函数", "refs" not in (poor.get("strategy") or {}))
both = pool.merge_entry_assets(
    E("d", "D", strategy={"refs": ["r1.png", "r3.png"]}),
    E("e", "E", strategy={"refs": ["r1.png", "r2.png"]}))
check("两批参考图取并集且去重",
      both["strategy"]["refs"] == ["r1.png", "r3.png", "r2.png"])

# ---------- 3) 并查集重复扫描 ----------
packs = [
    {"pack": "p1", "entries": [
        E("x1", "角色甲", "作品A"),     # 同 id 组
        E("y1", "角色乙", "作品B"),     # 同名同作品组
    ]},
    {"pack": "p2", "entries": [
        E("x1", "角色甲", "作品A"),     # 与 p1 同 id
        E("y2", "角色乙", "作品B"),     # 与 p1 同名同作品、id 不同
        E("z1", "独苗", "作品C"),
    ]},
]
groups = pool.duplicate_groups(packs)
check("扫出 2 组重复", len(groups) == 2, str([g["key"] for g in groups]))
check("每组恰好 2 条", all(len(g["items"]) == 2 for g in groups))
check("同 id 组标注正确", any("同 id" in g["reasons"] for g in groups))
check("同名同作品组标注正确",
      any("同名同作品" in g["reasons"] for g in groups))
check("独苗不成组",
      all(m["entry"]["id"] != "z1" for g in groups for m in g["items"]))
check("一个条目只属于一组",
      len({(m["pack"], m["index"]) for g in groups for m in g["items"]}) == 4)
check("扫描结果决定论", pool.duplicate_groups(packs) == groups)
clean = [{"pack": "only", "entries": [E("a", "A", "X"), E("b", "B", "Y")]}]
check("无重复时返回空", pool.duplicate_groups(clean) == [])
# 同名但作品不同不算重复
diff_series = [{"pack": "p", "entries": [E("a", "同名", "X"), E("b", "同名", "Y")]}]
check("同名不同作品不误判", pool.duplicate_groups(diff_series) == [])

# ---------- 4) 立绘归属迁移：不删行、不动文件 ----------
with tempfile.TemporaryDirectory() as td:
    s = store.Store(Path(td) / "t.db")
    for seq in range(3):
        s.add_portrait("old_id", f"portraits/old/{seq}.png", 1,
                       "2026-11-13", seq, "base")
    s.add_portrait("keep_id", "portraits/keep/0.png", 2,
                   "2026-11-13", 0, "base")
    check("迁移前各归各", s.count_portraits("old_id") == 3
          and s.count_portraits("keep_id") == 1)
    n = s.reassign_portraits("old_id", "keep_id")
    check("迁移返回行数", n == 3, str(n))
    check("迁移后立绘全在保留 id 下", s.count_portraits("old_id") == 0
          and s.count_portraits("keep_id") == 4)
    check("迁移不删行（总数守恒）", len(s.list_portraits_all()) == 4)
    check("相同 id 迁移是空操作", s.reassign_portraits("keep_id", "keep_id") == 0)
    s.close()

# ---------- 5) 接口集成：跨卷去重导入 / 安全覆盖 / 清理不丢资产 ----------
import asyncio  # noqa: E402
import json  # noqa: E402
import webui_api  # noqa: E402


class FakePlugin:
    def __init__(self, root, st):
        self._data_dir = root
        self._store = st
        self.context = object()


async def _call(handler, plugin, body):
    async def fake_body():
        return body
    real = webui_api._body
    webui_api._body = fake_body
    try:
        return await handler(plugin)
    finally:
        webui_api._body = real


def _dump(path, pack, entries):
    path.write_text(json.dumps({"pack": pack, "title": pack, "enabled": True,
                                "entries": entries}, ensure_ascii=False),
                    encoding="utf-8")


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    pools = root / "pools"
    pools.mkdir()
    st = store.Store(root / "t.db")
    plugin = FakePlugin(root, st)
    a_assets = {"type": "img2img", "ref": "refs/main.png",
                "refs": ["refs/1.png"]}
    _dump(pools / "game.json", "game", [
        E("a", "角色甲", "作品A", verified=True, cover="cover.png",
          strategy=dict(a_assets)),
        E("b", "角色乙", "作品B"),
    ])
    # 模拟 v0.22.0 导入残留的旧卷：a 同 id 重复、b2 同名同作品不同 id
    _dump(pools / "old.json", "old", [
        E("a", "角色甲", "作品A"),
        E("b2", "角色乙", "作品B",
          strategy={"refs": ["refs/old.png"]}),
    ])
    for seq in range(1):
        st.add_portrait("a", f"portraits/a/{seq}.png", 1, "2026-11-13", seq, "base")
    for seq in range(3):
        st.add_portrait("b", f"portraits/b/{seq}.png", 1, "2026-11-13", seq, "base")
    st.add_portrait("b2", "portraits/b2/0.png", 1, "2026-11-13", 0, "base")

    # 5.1 默认导入：全库跨卷按 id 去重
    payload = json.dumps([E("a", "角色甲NEW", "作品A-NEW"),
                          E("new1", "新角色", "作品C")], ensure_ascii=False)
    r1 = asyncio.run(_call(webui_api.h_import_pack, plugin,
                           {"text": payload, "pack": "imported"}))
    d1 = r1["data"]
    check("导入跳过跨卷已存在的 id",
          d1["imported"] == 1 and d1["duplicates"] == 1, str(d1))
    a_org = next(e for e in _load(pools / "game.json")["entries"] if e["id"] == "a")
    check("默认不覆盖：原条目基础信息也没动", a_org["name"] == "角色甲")

    # 5.2 开启覆盖：只改基础字段，参考图/封面/验证状态全留
    payload2 = json.dumps([E("a", "角色甲NEW", "作品A-NEW", verified=False,
                             strategy={"type": "base"})], ensure_ascii=False)
    d2 = asyncio.run(_call(webui_api.h_import_pack, plugin,
                           {"text": payload2, "pack": "imported",
                            "overwrite": True}))["data"]
    check("覆盖返回 updated=1", d2["updated"] == 1, str(d2))
    a_new = next(e for e in _load(pools / "game.json")["entries"] if e["id"] == "a")
    check("覆盖更新基础信息", a_new["name"] == "角色甲NEW"
          and a_new["series"] == "作品A-NEW")
    check("覆盖保住参考图/封面/验证状态",
          a_new["strategy"] == a_assets and a_new["cover"] == "cover.png"
          and a_new["verified"] is True, str(a_new))

    # 5.3 同名同作品但 id 不同：不自动合并
    d3 = asyncio.run(_call(webui_api.h_import_pack, plugin,
                           {"text": json.dumps([E("b3", "角色乙", "作品B")],
                                               ensure_ascii=False),
                            "pack": "imported"}))["data"]
    check("同名同作品不同 id 也被拦下", d3["duplicates"] == 1, str(d3))

    # 5.4 预览：2 组、保留资产最全的一条
    groups = asyncio.run(_call(webui_api.h_duplicates, plugin, {}))["data"]["groups"]
    check("预览扫出 2 组重复", len(groups) == 2, str([g["key"] for g in groups]))
    check("预览带 reasons_text / title / asset_text",
          all(g.get("reasons_text") and g.get("title")
              and all(it.get("asset_text") for it in g["items"]) for g in groups))
    ga = next(g for g in groups if any(it["id"] == "a" for it in g["items"]))
    check("同 id 组保留有资产的那条（game 卷）",
          ga["items"][ga["keep"]]["pack"] == "game", str(ga))
    gb = next(g for g in groups if any(it["id"] == "b2" for it in g["items"]))
    check("同名组保留立绘多的 b（game 卷）",
          gb["items"][gb["keep"]]["id"] == "b", str(gb))

    # 5.5 执行清理：立绘迁移 + 参考图并入 + 备份 + 可重扫
    total_before = len(st.list_portraits_all())
    d5 = asyncio.run(_call(webui_api.h_dedupe, plugin,
                           {"keys": [g["key"] for g in groups],
                            "confirm": True}))["data"]
    check("清理移除 2 条重复条目", d5["removed"] == 2, str(d5))
    check("被移除条目的立绘迁移到保留 id", d5["moved_portraits"] == 1, str(d5))
    check("立绘一行不少", len(st.list_portraits_all()) == total_before)
    kept_b = next(e for e in _load(pools / "game.json")["entries"] if e["id"] == "b")
    check("被移除条目的参考图并入保留条目",
          "refs/old.png" in (kept_b.get("strategy") or {}).get("refs", []),
          str(kept_b))
    check("备份目录已生成", Path(d5["backup"]).is_dir())
    check("清理后重扫无重复",
          asyncio.run(_call(webui_api.h_duplicates, plugin, {}))["data"]["total_groups"] == 0)
    # 未确认时不许执行
    d6 = asyncio.run(_call(webui_api.h_dedupe, plugin,
                           {"keys": ["whatever"]}))
    check("未 confirm 直接拒绝", d6["status"] == "error", str(d6))
    st.close()

print("test_dedupe: ALL OK")
