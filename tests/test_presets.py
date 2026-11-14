# -*- coding: utf-8 -*-
"""「素材库」自定义主题 / 维度词表守卫（直接运行，退出码即结论）。

v0.43.0：主题、模板、场景、维度词表以前全是 prompt_gen.py 的硬编码常量，
用户想调只能改源码重打包。现在独立落到 data_dir/waifu_presets.json，
由 provider 在运行时合并进出图链路。这里同时验：
  ① 无自定义时行为逐位不变（不能把既有出图结果洗牌）；
  ② 挂 provider 后自定义主题 / 短语真的进入 get_theme / 轮换池 / 抽签池；
  ③ 内置主题/短语可停用、自定义项可增删改，非法输入被规范化挡掉；
  ④ 保存 handler 写盘 → reload → provider 立刻生效（真跑 asyncio + 临时目录）。
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import batch_fill  # noqa: E402
import prompt_gen  # noqa: E402
import webui_api  # noqa: E402

PAGE = (ROOT / "pages" / "waifu-console" / "index.html").read_text(encoding="utf-8")
API = (ROOT / "webui_api.py").read_text(encoding="utf-8")
PROMPT = (ROOT / "prompt_gen.py").read_text(encoding="utf-8")

FAILURES = []


def check(name, cond, detail=""):
    if not cond:
        FAILURES.append(name)
        print(f"FAIL: {name} {detail}")
    else:
        print(f"ok: {name}")


# ---------- 1. 静态：全链路接线 ----------
check("menuOpts 含素材库页签", '{ label: "素材库", key: "presets" }' in PAGE)
check("后端页签白名单含 presets", '"bind", "presets", "config"' in API)
check("register 挂了 provider", "prompt_gen.set_custom_presets(lambda: _PRESETS_CACHE)" in API)
check("routes 注册 /presets 读写两条",
      API.count('("/presets", h_presets_') == 2)
check("h_themes 走合并后的 all_themes",
      "for t in prompt_gen.all_themes()]" in API)
check("prompt_gen 有 provider / 合并函数",
      all(f"def {n}(" in PROMPT for n in
          ("set_custom_presets", "_custom", "all_themes", "_pool_for")))
check("抽签 / 锁定 / 下拉都走 _pool_for",
      PROMPT.count("_pool_for(dim)") >= 4)
check("批量补图轮换池兼容测试桩（getattr 兜底）",
      'getattr(prompt_gen, "all_themes", None)' in
      (ROOT / "batch_fill.py").read_text(encoding="utf-8"))
check("配置页补上漏掉的 batch_require_ref", '"batch_require_ref"' in PAGE)
# 真机踩过：workflows ref 一直没暴露给模板，新页里直接 workflows.length 会
# 让整个 Vue 渲染抛 TypeError → 右侧整片空白，而 Vue.compile / node --check
# 都看不出来（它们只验语法，不验 setup return 的绑定是否齐）。
check("workflows 已暴露给模板（防白屏）", "workflows: workflows," in PAGE)
check("保存请求串行化（防快速连点乱序覆盖）",
      "_presetsSaveChain = _presetsSaveChain.then(" in PAGE)

# ---------- 2. 纯函数：无 provider 时行为不变 ----------
prompt_gen.set_custom_presets(None)
base_themes = prompt_gen.content_theme_ids()
base_pools = prompt_gen.dim_pool_options()
check("内置主题数 = THEMES 长度",
      len(prompt_gen.all_themes()) == len(prompt_gen.THEMES))
check("无自定义时轮换池与旧口径一致",
      base_themes == [str(t["id"]) for t in prompt_gen.THEMES
                      if t["id"] != "random"
                      and not str(t["id"]).startswith(("tpl_", "scene_"))],
      f"n={len(base_themes)}")
def _sorted_pool(v):
    ne = [it for it in v if str(it.get("zh") or "").strip()]
    ne.sort(key=lambda it: -int(it.get("w", 1)))
    return [str(it.get("zh") or "") for it in ne]


check("无自定义时维度池 == DIMS 的非空短语（权重降序，逐位一致）",
      all(base_pools[d] == _sorted_pool(v) for d, v in prompt_gen.DIMS.items()))

# ---------- 3. provider：合并 / 停用 ----------
CUSTOM = {
    "themes": [
        {"id": "usr_summer", "label": "夏日海边", "group": "content",
         "enabled": True, "outfit_kw": ["泳装"], "bg_kw": ["海边"],
         "plain_dims": [], "fixed_dims": {}},
        {"id": "usr_off", "label": "临时关掉", "group": "art", "enabled": False},
    ],
    "disabled_builtin": ["campus"],
    "dim_custom": {"outfit": ["我的自定义泳装"]},
    "dim_disabled": {"pose": [str(it.get("zh") or "")
                             for it in prompt_gen.DIMS["pose"]
                             if str(it.get("zh") or "").strip()][:1]},
}
prompt_gen.set_custom_presets(lambda: CUSTOM)

check("自定义主题进 all_themes",
      any(t["id"] == "usr_summer" for t in prompt_gen.all_themes()))
check("停用的内置主题不再出现",
      not any(t["id"] == "campus" for t in prompt_gen.all_themes()))
check("停用的自定义主题不进 all_themes",
      not any(t["id"] == "usr_off" for t in prompt_gen.all_themes()))
check("get_theme 能取到自定义主题",
      (prompt_gen.get_theme("usr_summer") or {}).get("label") == "夏日海边")
check("get_theme 取不到被停用的内置主题",
      prompt_gen.get_theme("campus") is None)
check("自定义主题进批量补图轮换池", "usr_summer" in prompt_gen.content_theme_ids())
check("被停用内置主题退出轮换池", "campus" not in prompt_gen.content_theme_ids())
check("batch_fill 轮换池与 prompt_gen 完全一致",
      batch_fill.content_theme_ids() == prompt_gen.content_theme_ids())
check("自定义短语进维度下拉",
      "我的自定义泳装" in prompt_gen.dim_pool_options()["outfit"])
check("自定义短语进抽签池",
      any(it.get("zh") == "我的自定义泳装"
          for it in prompt_gen._pool_for("outfit")))
check("被禁用的内置短语退出抽签池",
      CUSTOM["dim_disabled"]["pose"][0] not in
      [str(it.get("zh") or "") for it in prompt_gen._pool_for("pose")])
check("_fixed_item 认得自定义短语（en 兜底 zh）",
      prompt_gen._fixed_item("outfit", "我的自定义泳装")["en"] == "我的自定义泳装")
check("resolve_theme「随机」能摇中自定义主题",
      any(prompt_gen.resolve_theme("random", s)[1] == "usr_summer"
          for s in range(200)))
check("resolve_theme 同种子恒定（决定论不被 provider 破坏）",
      prompt_gen.resolve_theme("random", 12345) ==
      prompt_gen.resolve_theme("random", 12345))

# ---------- 4. 后端规范化（脏数据挡在外面） ----------
n = webui_api._normalize_presets({
    "themes": [
        {"id": "usr_ok", "label": " 正常 ", "group": "scene",
         "outfit_kw": "围裙, 白色\n,围裙", "plain_dims": ["outfit", "BAD_DIM"]},
        {"id": "scene_hack", "label": "撞内置前缀"},
        {"id": "usr_noname", "label": ""},
        {"id": "usr_ok", "label": "重复 id 取最后一条",
         "outfit_kw": "围裙, 白色\n,围裙", "plain_dims": ["outfit", "BAD_DIM"]},
    ],
    "disabled_builtin": ["campus", "NOT_EXIST"],
    "dim_custom": {"outfit": ["自定义A", "自定义A"], "BAD": ["x"]},
    "dim_disabled": {"pose": ["某个内置短语"]},
})
check("非 usr_ 前缀 / 空名被拒",
      [t["id"] for t in n["themes"]] == ["usr_ok"])
check("重复 id 去重（保留最后一条）",
      n["themes"][0]["label"] == "重复 id 取最后一条")
check("停用列表只保留内置 id", n["disabled_builtin"] == ["campus"])
check("plain_dims 白名单过滤", n["themes"][0]["plain_dims"] == ["outfit"])
check("*_kw 按行/逗号切分并去重",
      n["themes"][0]["outfit_kw"] == ["围裙", "白色"],
      str(n["themes"][0]["outfit_kw"]))
check("dim_custom 只保留已知维度并去重",
      n["dim_custom"] == {"outfit": ["自定义A"]}, str(n["dim_custom"]))
check("dim_disabled 只保留已知维度",
      list(n["dim_disabled"]) == ["pose"])

# ---------- 4b. v0.43.3：固定短语（fixed_dims）= 可编辑的提示词 ----------
# 旧版只认 zh，纯英文（danbooru 串）写的自定义固定短语会被静默丢掉。
n2 = webui_api._normalize_presets({
    "themes": [{
        "id": "usr_fd", "label": "固定短语",
        "fixed_dims": {
            "outfit": {"zh": "白色围裙", "en": "white apron"},
            "pose": {"zh": "", "en": "sitting on floor"},
            "bg": {"zh": "", "en": ""},
            "BAD_DIM": {"zh": "x", "en": "y"},
        },
    }],
})
_fd = n2["themes"][0]["fixed_dims"]
check("fixed_dims 中英任一非空即保留（英文工作流不再被丢）",
      (_fd.get("pose") or {}).get("en") == "sitting on floor", str(_fd))
check("fixed_dims 保留中文短语", (_fd.get("outfit") or {}).get("zh") == "白色围裙")
check("fixed_dims 中英都空 / 未知维度被丢弃",
      "bg" not in _fd and "BAD_DIM" not in _fd, str(_fd))
# 前端：固定短语从「手写 JSON」改成逐条编辑（维度 + 中文 + 英文）
check("素材库编辑器有固定短语逐行编辑",
      "presetsFixedRows" in PAGE and "addFixedRow" in PAGE
      and "removeFixedRow" in PAGE and "fixed_dims_text" not in PAGE)
check("内置主题也能编辑（编辑自动复制为自定义副本）",
      "function editPresetTheme" in PAGE and "editPresetTheme(t)" in PAGE)
check("主题表展示提示词概况（固定几维 / 关键词几组）", "promptText" in PAGE)

# ---------- 4c. v0.43.3：详情页生成面板默认「图生图重绘」 ----------
check("详情页默认图生图重绘（无源时回落补一张）",
      "function applyDefaultGalMode" in PAGE
      and 'genSources.value.length > 0 ? "img2img" : "refill"' in PAGE
      and "applyDefaultGalMode();" in PAGE)

# ---------- 5. handler 真跑：写盘 → reload → provider 生效 ----------
class Plugin:
    def __init__(self, data_dir):
        self._data_dir = str(data_dir)


with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    plugin = Plugin(td)
    webui_api.reload_presets(plugin)
    prompt_gen.set_custom_presets(lambda: webui_api._PRESETS_CACHE)

    async def run():
        got = await webui_api.h_presets_get(plugin)
        return got["data"]

    data = asyncio.run(run())
    check("GET /presets 返回 50 条内置主题与 11 个维度池",
          len(data["builtin_themes"]) == len(prompt_gen.THEMES)
          and len(data["dim_pools"]) == len(prompt_gen.DIMS))
    check("GET /presets 内置主题带分类",
          all(t.get("group") in ("content", "tpl", "art", "scene")
              for t in data["builtin_themes"]))

    payload = {"presets": {
        "themes": [{"id": "usr_from_api", "label": "API 建的主题",
                    "group": "content", "enabled": True,
                    "outfit_kw": ["泳装"]}],
        "disabled_builtin": ["campus"],
        "dim_custom": {"outfit": ["API 自定义短语"]},
    }}
    real_body = webui_api._body

    async def fake_body():
        return payload

    webui_api._body = fake_body
    try:
        saved = asyncio.run(webui_api.h_presets_set(plugin))["data"]
    finally:
        webui_api._body = real_body
    check("POST /presets 落盘成功",
          (td / "waifu_presets.json").is_file()
          and saved["themes"] == len(prompt_gen.THEMES) - 1 + 1)
    disk = json.loads((td / "waifu_presets.json").read_text(encoding="utf-8"))
    check("落盘内容是规范化后的结构",
          disk.get("version") == 1 and len(disk["themes"]) == 1)
    check("保存后 provider 立刻生效（get_theme）",
          (prompt_gen.get_theme("usr_from_api") or {}).get("label") == "API 建的主题")
    check("保存后内置停用生效", prompt_gen.get_theme("campus") is None)
    check("保存后自定义短语进下拉",
          "API 自定义短语" in prompt_gen.dim_pool_options()["outfit"])
    check("h_themes 返回合并后的主题列表",
          any(t["id"] == "usr_from_api"
              for t in asyncio.run(webui_api.h_themes(plugin))["data"]["themes"]))

# 收尾：别把 provider 留给同一进程里的其它用例
prompt_gen.set_custom_presets(None)
check("清理 provider 后回到纯内置",
      len(prompt_gen.all_themes()) == len(prompt_gen.THEMES))

if FAILURES:
    print(f"test_presets: {len(FAILURES)} FAILED")
    sys.exit(1)
print("test_presets: ALL OK")
