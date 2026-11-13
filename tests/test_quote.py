# -*- coding: utf-8 -*-
"""印象语 自检（可直接运行，退出码即结论；不联网、不依赖 astrbot）。

守卫点：
  1) /pools/quote_save 只改 quote / quotes——参考图绑定、封面、验证状态、画法
     一个都不能丢（v0.10.x「整条覆盖」旧坑的回归防线）；
  2) 清空印象语是合法操作（quotes=[] 且 quote 键被移除）；
  3) /quote_check 逐句一一对应返回，模型漏项时用原句兜底（句子绝不凭空消失）、
     越界条目不写库；
  4) websearch 不可用 / 关掉联网时降级 llm 且不报错；
  5) /pools/import 的 series 覆盖（作品页批量导入）只改作品名，别的字段不动。
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import webui_api  # noqa: E402


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL: {name} {detail}")
        sys.exit(1)
    print(f"ok: {name}")


class FakePlugin:
    def __init__(self, root):
        self._data_dir = root
        self._store = None
        self.context = object()

    def _cfg(self, key, default=None):
        return default


async def _call(handler, plugin, body):
    async def fake_body():
        return body
    real_body = webui_api._body
    webui_api._body = fake_body
    try:
        return await handler(plugin)
    finally:
        webui_api._body = real_body


def _dump(path, pack, entries):
    path.write_text(json.dumps({"pack": pack, "title": pack, "enabled": True,
                                "entries": entries}, ensure_ascii=False),
                    encoding="utf-8")


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def E(cid, name, series="某作", **kw):
    d = {"id": cid, "name": name, "tags_en": "1girl", "series": series,
         "verified": True, "strategy": {"type": "img2img",
                                        "ref": "refs/main.png",
                                        "refs": ["refs/1.png"]},
         "cover": "cover.png"}
    d.update(kw)
    return d


class FakeResp:
    def __init__(self, text):
        self.completion_text = text


def _fake_provider(reply_text, calls=None):
    class Prov:
        async def text_chat(self, prompt="", session_id=None, system_prompt=""):
            if calls is not None:
                calls.append({"prompt": prompt, "system": system_prompt})
            return FakeResp(reply_text)
    return Prov()


with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    pools = root / "pools"
    pools.mkdir(parents=True)
    pack_path = pools / "game.json"
    _dump(pack_path, "game", [E("a", "角色甲"), E("b", "角色乙", quotes=["原句一"])])
    plugin = FakePlugin(root)

    # ---------- 1) 只改 quote / quotes ----------
    r = asyncio.run(_call(webui_api.h_quote_save, plugin,
                          {"id": "a", "quotes": ["新句一", "新句二"]}))
    check("保存印象语返回 ok", r["status"] == "ok", str(r))
    a = next(e for e in _load(pack_path)["entries"] if e["id"] == "a")
    check("quotes 落盘且去重", a["quotes"] == ["新句一", "新句二"], str(a.get("quotes")))
    check("quote 跟着取第一句（卡片渲染读它）", a["quote"] == "新句一")
    check("参考图绑定原封不动",
          a["strategy"] == {"type": "img2img", "ref": "refs/main.png",
                            "refs": ["refs/1.png"]}, str(a.get("strategy")))
    check("封面 / 验证状态不动",
          a["cover"] == "cover.png" and a["verified"] is True)

    # 按行文本也接受（详情页 textarea 直接传原文）
    r = asyncio.run(_call(webui_api.h_quote_save, plugin,
                          {"id": "a", "quotes": "  行一  \n\n行二\n行一 "}))
    a = next(e for e in _load(pack_path)["entries"] if e["id"] == "a")
    check("按行文本解析 + 去空行去重", a["quotes"] == ["行一", "行二"], str(a.get("quotes")))

    # ---------- 2) 清空 ----------
    r = asyncio.run(_call(webui_api.h_quote_save, plugin, {"id": "a", "quotes": []}))
    a = next(e for e in _load(pack_path)["entries"] if e["id"] == "a")
    check("清空后 quotes 为空且 quote 键移除",
          a.get("quotes") == [] and "quote" not in a, str(a))
    check("清空不影响参考图",
          (a.get("strategy") or {}).get("ref") == "refs/main.png")

    # ---------- 3) 参数校验 ----------
    check("缺 id 被拒",
          asyncio.run(_call(webui_api.h_quote_save, plugin, {"quotes": []}))["status"]
          == "error")
    check("缺 quotes 被拒",
          asyncio.run(_call(webui_api.h_quote_save, plugin, {"id": "a"}))["status"]
          == "error")
    check("不存在的 id 被拒",
          asyncio.run(_call(webui_api.h_quote_save, plugin,
                            {"id": "nope", "quotes": ["x"]}))["status"] == "error")

    # ---------- 4) 校对：一一对应 + 漏项兜底 ----------
    real_resolve = webui_api.prompt_gen._resolve_provider
    real_search = webui_api._web_search_text
    calls = []

    async def fake_resolve(context, provider_id=""):
        return _fake_provider(
            json.dumps([{"text": "改后一", "note": "加了身份"},
                        {"text": "", "note": ""}], ensure_ascii=False), calls)

    async def fake_search(p, query, max_results=10, timeout=25):
        check("联网校对带上了角色名", "角色甲" in query, query)
        return "检索摘要：角色甲是某作的女主角，说话很温柔。"

    webui_api.prompt_gen._resolve_provider = fake_resolve
    webui_api._web_search_text = fake_search
    try:
        r = asyncio.run(_call(webui_api.h_quote_check, plugin,
                              {"name": "角色甲", "series": "某作",
                               "quotes": ["原句一", "原句二"], "web": True}))
    finally:
        webui_api.prompt_gen._resolve_provider = real_resolve
        webui_api._web_search_text = real_search
    d = r["data"]
    check("校对返回 ok 且标注 websearch+llm", r["status"] == "ok"
          and d["source"] == "websearch+llm", str(r))
    check("逐句一一对应（条数不变）", len(d["items"]) == 2, str(d["items"]))
    check("改写的句子保留模型给出文本", d["items"][0]["text"] == "改后一")
    check("旧句原样回传（前端按 old 替换）", d["items"][0]["old"] == "原句一")
    check("模型漏项用原句兜底，句子不消失", d["items"][1]["text"] == "原句二")
    check("changed 只数真正改动的句子", d["changed"] == 1, str(d["changed"]))
    check("检索摘要进了提示词", any("检索摘要" in c["prompt"] for c in calls))
    check("校对不写库（等用户点采用再保存）",
          next(e for e in _load(pack_path)["entries"] if e["id"] == "b")["quotes"]
          == ["原句一"])

    # 联网失败/关闭 → 降级 llm，不报错
    async def empty_search(p, query, max_results=10, timeout=25):
        return ""

    async def fake_resolve2(context, provider_id=""):
        return _fake_provider('[{"text": "原句一", "note": "保留"}]')

    webui_api.prompt_gen._resolve_provider = fake_resolve2
    webui_api._web_search_text = empty_search
    try:
        r = asyncio.run(_call(webui_api.h_quote_check, plugin,
                              {"name": "角色乙", "quotes": ["原句一"], "web": True}))
        r2 = asyncio.run(_call(webui_api.h_quote_check, plugin,
                               {"name": "角色乙", "quotes": [], "web": False}))
    finally:
        webui_api.prompt_gen._resolve_provider = real_resolve
        webui_api._web_search_text = real_search
    check("无检索结果时降级 llm 且仍可用",
          r["status"] == "ok" and r["data"]["source"] == "llm", str(r))
    check("没有印象语时明确拒绝", r2["status"] == "error", str(r2))

    # ---------- 5) 导入的 series 覆盖（作品页批量导入） ----------
    payload = json.dumps([E("c", "角色丙", "写错的作品"),
                          E("d", "角色丁", "")], ensure_ascii=False)
    r = asyncio.run(_call(webui_api.h_import_pack, plugin,
                          {"text": payload, "pack": "imported",
                           "series": "目标作品"}))
    d = r["data"]
    check("导入返回所归作品", d.get("series") == "目标作品", str(d))
    imported = _load(pools / "imported.json")["entries"]
    check("导入条目的作品名被统一改写",
          all(e["series"] == "目标作品" for e in imported), str(imported))
    check("series 覆盖不影响 id / name / tags",
          {e["id"] for e in imported} == {"c", "d"}
          and all(e["tags_en"] == "1girl" for e in imported))

    # 不传 series：完全按条目自带（角色库普通导入行为不变）
    payload2 = json.dumps([E("e", "角色戊", "自带作品")], ensure_ascii=False)
    asyncio.run(_call(webui_api.h_import_pack, plugin,
                      {"text": payload2, "pack": "imported2"}))
    kept = _load(pools / "imported2.json")["entries"]
    check("不传 series 时保留条目自带作品",
          kept[0]["series"] == "自带作品", str(kept))

# ---------- 6) websearch 工具解析（v0.29.1：4.29 改名 get_func 后曾静默失效） ----------


class _FakeTool:
    def __init__(self, name, reply, active=True, raises=False):
        self.name = name
        self.reply = reply
        self.active = active
        self.raises = raises
        self.calls = []

    async def call(self, ctx, query="", max_results=10):
        self.calls.append({"query": query, "max_results": max_results})
        if self.raises:
            raise RuntimeError("boom")
        return self.reply


class _MgrGetFunc:
    """AstrBot 4.29+：只有 get_func（没有 get_tool）。"""

    def __init__(self, tools):
        self._tools = {t.name: t for t in tools}
        self.used = []

    def get_func(self, name):
        self.used.append(name)
        return self._tools.get(name)


class _MgrGetTool:
    """旧版：只有 get_tool。"""

    def __init__(self, tools):
        self._tools = {t.name: t for t in tools}

    def get_tool(self, name):
        return self._tools.get(name)


class _MgrNone:
    pass


class _WsPlugin:
    def __init__(self, manager):
        self.context = _WsCtx(manager)

    def _cfg(self, key, default=None):
        return default


class _WsCtx:
    def __init__(self, manager):
        self._mgr = manager

    def get_config(self, umo=None):
        return {}

    def get_llm_tool_manager(self):
        return self._mgr


ok_tool = _FakeTool("web_search_tavily", '{"results":[{"title":"角色设定","snippet":"她是某作的女主角，性格温柔"}]}')
txt = asyncio.run(webui_api._web_search_text(
    _WsPlugin(_MgrGetFunc([ok_tool])), "《某作》角色甲 角色设定"))
check("get_func（4.29+）能取到内置 websearch 并返回摘要",
      "女主" in txt, txt)
check("查询串原样传给工具", ok_tool.calls and "《某作》" in ok_tool.calls[0]["query"],
      str(ok_tool.calls))

txt = asyncio.run(webui_api._web_search_text(
    _WsPlugin(_MgrGetTool([_FakeTool("web_search_bocha",
                                     "旧版 get_tool 也能取到的摘要内容，长度足够")])),
    "q"))
check("只认 get_tool 的旧版仍可用（向后兼容）", "旧版" in txt, txt)

txt = asyncio.run(webui_api._web_search_text(_WsPlugin(_MgrNone()), "q"))
check("既无 get_func 也无 get_tool → 空串降级", txt == "")

boom = _FakeTool("web_search_tavily", "", raises=True)
good = _FakeTool("web_search_bocha", "第二个工具给出的可用摘要，长度足够")
txt = asyncio.run(webui_api._web_search_text(_WsPlugin(_MgrGetFunc([boom, good])), "q"))
check("第一个工具抛异常时继续试下一个", "第二个工具" in txt, txt)

err = _FakeTool("web_search_tavily", "Error: Tavily API key is not configured in AstrBot.")
good2 = _FakeTool("web_search_brave", "某作角色甲的设定摘要，内容长度足够")
txt = asyncio.run(webui_api._web_search_text(_WsPlugin(_MgrGetFunc([err, good2])), "q"))
check("工具返回 Error 文本被视为失败", "某作角色甲" in txt, txt)

inactive = _FakeTool("web_search_tavily", "", active=False)
active = _FakeTool("web_search_bocha", "已激活工具的摘要内容长度足够")
txt = asyncio.run(webui_api._web_search_text(
    _WsPlugin(_MgrGetFunc([inactive, active])), "q"))
check("已激活工具优先，禁用的不抢先", "已激活工具" in txt and not inactive.calls, txt)

# ---------- 7) 查询串锚定作品（同名角色串台的防线） ----------
q = webui_api._character_query("角色甲", "某作", "角色设定")
check("有作品时作品名打头并带书名号", q.startswith("《某作》角色甲"), q)
q2 = webui_api._character_query("角色甲", "", "角色设定")
check("没作品时退化为名字 + 动漫角色", q2.startswith("角色甲 动漫 角色"), q2)
brief = webui_api._character_brief("角色甲", "某作", "1girl")
check("角色头写明作品并以该作品为准", "作品：某作" in brief and "以这部作品为准" in brief, brief)
brief2 = webui_api._character_brief("角色甲", "", "")
check("无作品时明确告知无法确认设定", "没有填作品名" in brief2, brief2)
check("检索摘要段要求以摘要为准并剔除同名噪声",
      "以本摘要为准" in webui_api._search_block("x")
      and "另一部作品" in webui_api._search_block("x"))

# ---------- 8) 纯函数 ----------
check("_clean_quote_lines 去空白/去重/截断",
      webui_api._clean_quote_lines([" 一 ", "一", "", "二"]) == ["一", "二"])
check("_clean_quote_lines 容忍非列表", webui_api._clean_quote_lines(123) == [])
check("sanitize 掉编号（模型爱加 1.）",
      webui_api._clean_quote_lines(["1. 你好"]) == ["你好"],
      str(webui_api._clean_quote_lines(["1. 你好"])))

print("test_quote: ALL OK")
