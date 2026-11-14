# -*- coding: utf-8 -*-
"""角色资料导入 自检（可直接运行，退出码即结论；不联网、不依赖 astrbot）。

守卫点：
  ① 解析容错——单对象 / 数组 / {entries} 三种形态、中文键别名、印象语按行；
  ② 匹配三档——id > 名字+作品 > 全库唯一名字；
  ③ 冲突策略——skip 只补空（已有内容一个字都不改）、overwrite 才覆盖；
  ④ 维度开关——未启用的维度绝不写库；
  ⑤ 未匹配新建——有 tags_en 才建，且不碰其它卷；
  ⑥ 资产安全——strategy / cover / verified / avatar 原样保留。
"""
import asyncio
import json
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


def E(cid, name, series="某作", **kw):
    d = {"id": cid, "name": name, "tags_en": "1girl", "series": series,
         "verified": False, "strategy": {}, "cover": ""}
    d.update(kw)
    return d


class FakePlugin:
    def __init__(self, root, st):
        self._data_dir = root
        self._store = st
        self.context = object()

    def _today(self):
        return "2026-11-15"


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


def _entry(path, cid):
    return next(e for e in _load(path)["entries"] if e["id"] == cid)


# ---------- 1) 解析：三种形态 + 中文键 + 印象语 ----------
items, pack_hint, series_hint, bad = webui_api._profile_import_parse(json.dumps({
    "name": "薄荷", "series": "异环",
    "profile": {"bio": "猫娘", "生日": "6月1日", "不认识的键": "丢弃"},
    "印象语": "呀嚯！\n可可！",
    "评级": "s",
}, ensure_ascii=False))
check("单对象也能解析", len(items) == 1 and not bad)
check("中文键别名认得出",
      items[0]["incoming"].get("birthday") == "6月1日"
      and items[0]["incoming"].get("bio") == "猫娘")
check("未知键被丢弃", "不认识的键" not in items[0]["incoming"])
check("印象语按行切分", items[0]["incoming"].get("quotes") == ["呀嚯！", "可可！"])
check("评级归一成大写", items[0]["incoming"].get("tier") == "S")

arr_items, _, _, _ = webui_api._profile_import_parse(json.dumps(
    [{"name": "A", "bio": "x"}, {"name": "B", "bio": "y"}], ensure_ascii=False))
check("数组解析出 2 条", len(arr_items) == 2)

env_items, hint_pack, hint_series, _ = webui_api._profile_import_parse(json.dumps({
    "pack": "异环卷", "series": "异环", "entries": [{"name": "A", "bio": "x"}],
}, ensure_ascii=False))
check("整卷对象带出 pack / series 提示",
      len(env_items) == 1 and hint_pack == "异环卷" and hint_series == "异环")

bad_items, _, _, bad_list = webui_api._profile_import_parse(json.dumps(
    [{"name": "A", "bio": "x"}, {"foo": 1}, "不是对象"], ensure_ascii=False))
check("坏条目进 bad 不拖垮整批",
      len(bad_items) == 1 and len(bad_list) == 2, str(bad_list))

try:
    webui_api._profile_import_parse("{}")
    raise AssertionError("空对象应当报错")
except ValueError as exc:
    check("空对象给出人话错误", "没有" in str(exc), str(exc))

# ---------- 2) 预览：匹配三档 + 维度计数 ----------
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    root = Path(td)
    pools = root / "pools"
    pools.mkdir()
    st = store.Store(root / "t.db")
    plugin = FakePlugin(root, st)
    game_path = pools / "game.json"
    other_path = pools / "other.json"
    assets = {"type": "img2img", "ref": "refs/main.png",
              "refs": ["refs/1.png"], "denoise": 0.4}
    _dump(game_path, "game", [
        E("mint", "薄荷", "异环", verified=True, cover="cover.png",
          strategy=dict(assets), profile={"bio": "旧简介"},
          quotes=["旧台词"], tier="A"),
    ])
    _dump(other_path, "other", [
        E("nanally", "娜娜莉", "异环"),
    ])
    payload = json.dumps({"entries": [
        {"id": "mint", "bio": "新简介", "height": "152cm",
         "quotes": "新台词一\n新台词二", "tier": "S"},
        {"name": "娜娜莉", "series": "异环", "cv": "宋媛媛"},
        {"name": "早雾", "series": "异环", "tags_en": "1girl, twintails",
         "bio": "小霸王"},
        {"name": "无名氏", "series": "异环", "bio": "没有 tags 建不出来"},
    ]}, ensure_ascii=False)

    prev = asyncio.run(_call(webui_api.h_profile_import_preview, plugin,
                             {"text": payload}))
    check("预览成功", prev["status"] == "ok", str(prev)[:200])
    d = prev["data"]
    check("总条数 4", d["total"] == 4, str(d["total"]))
    by_name = {r["name"]: r for r in d["items"]}
    check("按 id 匹配", by_name["薄荷"]["matched"]
          and by_name["薄荷"]["match_by"] == "id"
          and by_name["薄荷"]["pack"] == "game")
    check("按名字+作品匹配", by_name["娜娜莉"]["matched"]
          and by_name["娜娜莉"]["match_by"] == "名字+作品")
    check("未匹配但有 tags_en 可新建", not by_name["早雾"]["matched"]
          and by_name["早雾"]["can_create"])
    check("未匹配且无 tags_en 不可新建", not by_name["无名氏"]["can_create"])
    check("预览带回当前值", by_name["薄荷"]["current"].get("bio") == "旧简介"
          and by_name["薄荷"]["current"].get("quotes") == ["旧台词"])
    fcnt = {f["key"]: f["count"] for f in d["fields"]}
    check("维度命中条数正确", fcnt["bio"] == 3 and fcnt["quotes"] == 1
          and fcnt["height"] == 1 and fcnt["tier"] == 1, str(fcnt))
    check("预览不写盘", _entry(game_path, "mint").get("profile") == {"bio": "旧简介"})

    # ---------- 3) skip：只补空 ----------
    r = asyncio.run(_call(webui_api.h_profile_import, plugin,
                          {"text": payload, "conflict": "skip"}))
    check("skip 模式执行成功", r["status"] == "ok", str(r)[:200])
    d = r["data"]
    mint = _entry(game_path, "mint")
    check("skip 不动已有简介", mint["profile"]["bio"] == "旧简介", str(mint))
    check("skip 补上空的身高", mint["profile"]["height"] == "152cm")
    check("skip 不动已有印象语", mint.get("quotes") == ["旧台词"])
    check("skip 不动已有评级", mint.get("tier") == "A")
    check("资产字段原样保留",
          mint["strategy"] == assets and mint["cover"] == "cover.png"
          and mint["verified"] is True, str(mint))
    nanally = _entry(other_path, "nanally")
    check("另一卷也按名字匹配写入", nanally["profile"].get("cv") == "宋媛媛")
    names = [e["id"] for e in _load(pools / "imported.json")["entries"]] \
        if (pools / "imported.json").exists() else []
    check("skip 也沿用默认不新建（create_missing 默认关）", names == [], str(names))
    check("跳过明细带原因",
          any(x["status"] == "skipped" and x.get("reason")
              for x in d["results"]))

    # ---------- 4) overwrite：覆盖已有 ----------
    r2 = asyncio.run(_call(webui_api.h_profile_import, plugin,
                           {"text": payload, "conflict": "overwrite",
                            "create_missing": True, "pack": "异环卷"}))
    d2 = r2["data"]
    mint = _entry(game_path, "mint")
    check("overwrite 覆盖简介", mint["profile"]["bio"] == "新简介", str(mint))
    check("overwrite 替换印象语",
          mint.get("quotes") == ["新台词一", "新台词二"]
          and mint.get("quote") == "新台词一", str(mint.get("quotes")))
    check("overwrite 覆盖评级", mint.get("tier") == "S")
    check("overwrite 仍不碰资产字段",
          mint["strategy"] == assets and mint["cover"] == "cover.png")
    check("create_missing 建出新角色", d2["created"] >= 1, str(d2["created"]))
    new_pack = _load(pools / "异环卷.json")
    zaowu = next(e for e in new_pack["entries"] if e["name"] == "早雾")
    check("新建条目带上 tags_en / 资料 / 作品",
          zaowu["tags_en"] == "1girl, twintails"
          and zaowu["profile"].get("bio") == "小霸王"
          and zaowu["series"] == "异环", str(zaowu))
    check("新建条目未验证（待人工过目）", zaowu["verified"] is False)
    check("新建条目有入库日期", bool(zaowu.get("added_at")))
    check("缺 tags_en 的条目不建", not any(e["name"] == "无名氏"
                                     for e in new_pack["entries"]))

    # ---------- 5) 维度开关 ----------
    payload2 = json.dumps([{"id": "mint", "bio": "只该写简介", "tier": "D",
                            "quotes": "不该写入"}], ensure_ascii=False)
    asyncio.run(_call(webui_api.h_profile_import, plugin,
                      {"text": payload2, "conflict": "overwrite",
                       "fields": ["bio"]}))
    mint = _entry(game_path, "mint")
    check("未启用的维度绝不写库", mint["profile"]["bio"] == "只该写简介"
          and mint.get("tier") == "S" and mint.get("quotes") == ["新台词一", "新台词二"],
          str(mint))

    # ---------- 6) only：只导勾选的条目 ----------
    payload3 = json.dumps([
        {"id": "mint", "memo": "勾选"},
        {"id": "nanally", "memo": "未勾选"},
    ], ensure_ascii=False)
    prev3 = asyncio.run(_call(webui_api.h_profile_import_preview, plugin,
                              {"text": payload3}))["data"]
    key_mint = next(r["key"] for r in prev3["items"] if r["id"] == "mint")
    asyncio.run(_call(webui_api.h_profile_import, plugin,
                      {"text": payload3, "conflict": "overwrite",
                       "only": [key_mint]}))
    check("勾选的写了", _entry(game_path, "mint")["profile"].get("memo") == "勾选")
    check("没勾的不动", "memo" not in (_entry(other_path, "nanally")["profile"]))

    # ---------- 7) 全空的导入被拒 ----------
    empty = asyncio.run(_call(webui_api.h_profile_import, plugin,
                              {"text": json.dumps([{"name": "x"}])}))
    check("没有任何维度时报错", empty["status"] == "error", str(empty))
    bad_json = asyncio.run(_call(webui_api.h_profile_import_preview, plugin,
                                 {"text": "{不是json"}))
    check("坏 JSON 报错不崩", bad_json["status"] == "error")
    st.close()

print("test_profile_import: ALL OK")
