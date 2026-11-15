# -*- coding: utf-8 -*-
"""v0.42.0 工坊改版回归守卫（直接运行，退出码即结论）。

盯住本轮 11 条用户反馈里后果最重 / 最容易复发的点：
  ① 抽卡播报绝不出现绘图 / 参考图字样（用户实报「照着参考图画」）；
  ② avatars.py 包内相对导入（No module named 'prompt_gen' 事故）；
  ③ 头像生成 mode / ref / apply 三参数 + 候选清理端点；
  ④ 重置今日记录不再清立绘；reset_char_day 真 SQL 最多删一行（十几张立绘
     一次性消失的事故）；
  ⑤ 调试查询历史（debug_queries + 进页即加载）；
  ⑥ 图生图默认选中指定图源 / 第一张；
  ⑦ 立绘数量只算启用、速览并入资料、新增图库页签；
  ⑧ 固定头部 / 新 tab 风格 / 总览 / 配置悬浮保存 / CRLF 行尾。
"""
import ast
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import store as store_mod  # noqa: E402

PAGE_PATH = ROOT / "pages" / "waifu-console" / "index.html"
PAGE = PAGE_PATH.read_text(encoding="utf-8")
PAGE_BYTES = PAGE_PATH.read_bytes()
MAIN = (ROOT / "main.py").read_text(encoding="utf-8")
AVATARS = (ROOT / "avatars.py").read_text(encoding="utf-8")
API = (ROOT / "webui_api.py").read_text(encoding="utf-8")
STORE = (ROOT / "store.py").read_text(encoding="utf-8")

FAILURES = []


def check(name, cond, detail=""):
    if not cond:
        FAILURES.append(name)
        print(f"FAIL: {name} {detail}")
    else:
        print(f"ok: {name}")


# ---------- ① 抽卡播报：只说抽卡，不抖出内部出图实现 ----------
tree = ast.parse(MAIN)
consts = {}
for node in tree.body:
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id.startswith("_") and t.id.endswith("_LINES"):
                try:
                    consts[t.id] = ast.literal_eval(node.value)
                except Exception:
                    pass
check("图生图专属抽卡台词 _I2I_LINES 已彻底删除", "_I2I_LINES" not in consts)
check("_summon_line 不再按 decision 分支", "_I2I_LINES" not in MAIN)
BAD_WORDS = ("画", "绘", "参考图", "出图", "生图", "照着", "对照")
spoken = (consts.get("_SUMMON_LINES") or []) + (consts.get("_QUEUE_LINES") or [])
dirty = [s for s in spoken if any(w in s for w in BAD_WORDS)]
check("所有抽卡提示都不含绘图 / 参考图字样", bool(spoken) and not dirty, str(dirty))
check("抽卡提示仍是多句随机池", len(consts.get("_SUMMON_LINES") or []) >= 4)

# ---------- ② avatars.py 包内导入 ----------
check("build_avatar_prompt 两处走相对导入 prompt_gen",
      AVATARS.count("from . import prompt_gen") == 2)
check("批量队列走相对导入 webui_api", "from . import webui_api" in AVATARS)
bare = list(re.finditer(r"^\s*import (?:prompt_gen|webui_api)\s*$", AVATARS, re.M))
check("所有绝对导入都只是 ImportError 平铺回退",
      bool(bare) and all("except ImportError" in AVATARS[max(0, m.start() - 220):m.start()]
                         for m in bare))

# ---------- ③ 头像生成后端契约 ----------
check("generate_avatar_for 支持 mode / ref / apply",
      re.search(r"async def generate_avatar_for\([^)]*mode[^)]*ref[^)]*apply",
                API, re.S) is not None)
check("文生图强制把 img2img 条目改成 base",
      'draw.effective_entry(use_entry, {"strategy_type": "base"})' in API)
check("apply=False 落 _pending_ 候选且不写条目",
      "_pending_" in API and "if apply:\n        def mutate" in API)
check("job 结果带 pending / applied", '"pending": "" if apply else rel' in API)
check("/avatar/discard 只清候选文件",
      '("/avatar/discard"' in API and 'not p.name.startswith("_pending_")' in API)
check("h_debug_history 返回完整查询流水", '"queries": queries' in API)
check("overview 立绘数只算启用行",
      'int(r.get("active") or 0) == 1' in API and '"portraits_disabled"' in API)

# ③b 真跑包内导入：把 avatars.py 以「包内子模块」身份加载，验证 qwen 分支
#     拿得到 prompt_gen（用户报的 No module named 'prompt_gen' 就出在这里）。
import asyncio  # noqa: E402
import importlib.util  # noqa: E402
import types  # noqa: E402

pkg_name = "_waifu_pkg_probe"
pkg = types.ModuleType(pkg_name)
pkg.__path__ = [str(ROOT)]
fake_pg = types.ModuleType(pkg_name + ".prompt_gen")
fake_pg.identity_feature_text = lambda entry, lang: "猫耳与尾巴"
pkg.prompt_gen = fake_pg
sys.modules[pkg_name] = pkg
sys.modules[pkg_name + ".prompt_gen"] = fake_pg
_spec = importlib.util.spec_from_file_location(pkg_name + ".avatars", ROOT / "avatars.py")
av_pkg = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = av_pkg
_spec.loader.exec_module(av_pkg)
try:
    _out = av_pkg.build_avatar_prompt({"name": "A", "tags_en": "cat ears"}, "qwen")
    check("包内 qwen 分支取得到 prompt_gen（不再 ModuleNotFoundError）",
          "猫耳与尾巴" in _out)
except Exception as exc:
    check("包内 qwen 分支取得到 prompt_gen（不再 ModuleNotFoundError）", False, repr(exc))

# ③c 真跑生成链路（桩掉萌绘调用）：i2i 带指定图源 / t2i 强制切 base /
#     apply=False 只落候选文件、绝不覆盖正式头像。
import webui_api as api  # noqa: E402
import draw as draw_mod  # noqa: E402
import avatars as avatars_mod  # noqa: E402
from PIL import Image  # noqa: E402

with tempfile.TemporaryDirectory() as td2:
    td2 = Path(td2)
    ref = td2 / "refs" / "main.png"
    ref.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (512, 768), (220, 180, 200)).save(ref)

    class Plugin:
        context = object()
        _data_dir = str(td2)

        def _draw_cfg(self):
            return {}

    plugin = Plugin()
    seen = {}
    real_gen = draw_mod.generate_portrait
    real_detect = api.prompt_gen.detect_t2i_style
    real_eff = api.prompt_gen.effective_workflow
    real_event = api._synthetic_event
    api._synthetic_event = lambda: None   # 本机没有 astrbot 运行时，别刷合成事件日志

    async def fake_gen(context, cfg, entry, seed, event, prompt=""):
        seen["entry"] = entry
        seen["prompt"] = prompt
        src = td2 / "fake_out.png"
        Image.new("RGB", (700, 1200), (200, 120, 180)).save(src)
        return str(src), "ok", "", 0

    draw_mod.generate_portrait = fake_gen
    api.prompt_gen.detect_t2i_style = lambda ctx, wf: "danbooru"
    api.prompt_gen.effective_workflow = lambda entry, cfg: ""
    try:
        entry = {"id": "c1", "name": "C", "tags_en": "cat ears",
                 "strategy_type": "base", "strategy": {}}
        rel, w, h = asyncio.run(api.generate_avatar_for(
            plugin, entry, mode="i2i", ref=str(ref), apply=False))
        check("i2i 强制切 img2img 且带上指定图源",
              seen["entry"]["strategy_type"] == "img2img"
              and str(seen["entry"]["strategy"]["ref"]).endswith("main.png")
              and seen["prompt"] == avatars_mod.I2I_INSTRUCTION)
        check("apply=False 只落候选、不覆盖正式头像",
              rel.startswith("avatars/_pending_")
              and (td2 / rel).is_file()
              and not (td2 / "avatars" / "c1.png").exists()
              and (w, h) == (512, 512))

        entry2 = {"id": "c2", "name": "D", "tags_en": "cat ears",
                  "strategy_type": "img2img",
                  "strategy": {"type": "img2img", "ref": str(ref)}}
        asyncio.run(api.generate_avatar_for(plugin, entry2, mode="t2i", apply=False))
        check("t2i 强制走文生图（img2img 条目临时切 base）",
              seen["entry"]["strategy_type"] == "base"
              and "cat ears" in seen["prompt"])
    finally:
        draw_mod.generate_portrait = real_gen
        api.prompt_gen.detect_t2i_style = real_detect
        api.prompt_gen.effective_workflow = real_eff
        api._synthetic_event = real_event

# ---------- ④ 重置不再清库：静态 + 真 SQL ----------
check("store.reset_char_day 不再按日期批量 DELETE",
      'DELETE FROM portraits WHERE char_id=? AND date=?' not in STORE)
check("store.reset_char_day 按主键最多删一行",
      "DELETE FROM portraits WHERE id=?" in STORE)
check("前端 clear_today 分支不再调 reset_char_day",
      (lambda m: bool(m) and "reset_char_day" not in m.group(1)
       and "debug/clear_draws" in m.group(1))(
          re.search(r'\} else if \(kind === "clear_today"\) \{(.*?)\} else if', PAGE, re.S)))
check("前端重置说明明确不动立绘", "不会动任何立绘" in PAGE)


def seed_portraits(st, cid, date, n):
    files = []
    for i in range(1, n + 1):
        f = f"portraits/{cid}/{date}_{i:03d}_{i:05d}.png"
        st._conn.execute(
            "INSERT INTO portraits (char_id, file, seed, date, seq, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (cid, f, 1000 + i, date, i, "2026-11-14T00:00:00Z"))
        files.append(f)
    st._conn.commit()
    return files


with tempfile.TemporaryDirectory() as td:
    st = store_mod.Store(Path(td) / "waifu.db")
    date = "2026-11-14"
    fs = seed_portraits(st, "charX", date, 5)
    st.upsert_draw("u1", date, "charX", 0, False, "nick", "g1", "群", "aiocqhttp",
                   seed_hex="", char_name="X", series="", portrait_file=fs[2],
                   decision="generate")
    st._conn.commit()
    res = st.reset_char_day("charX", date)
    check("reset 最多只删一张（同日 5 张批量补图）",
          len(res["removed_files"]) == 1, str(res))
    check("删的是当日签关联的那一张", res["removed_files"][0] == fs[2])
    left = st._conn.execute(
        "SELECT COUNT(*) AS n FROM portraits WHERE char_id='charX'").fetchone()["n"]
    check("其余 4 张立绘安然无恙", left == 4, f"剩 {left} 张")
    check("清签本身不碰 portraits 行（只删 draws/events）",
          "DELETE FROM portraits" not in STORE.split("def clear_draws")[1].split("def get_epoch")[0])

    fs2 = seed_portraits(st, "charY", date, 5)   # 无关联签
    res2 = st.reset_char_day("charY", date)
    check("无关联签时退化成只删最新一张",
          len(res2["removed_files"]) == 1 and res2["removed_files"][0] == fs2[-1],
          str(res2))
    left2 = st._conn.execute(
        "SELECT COUNT(*) AS n FROM portraits WHERE char_id='charY'").fetchone()["n"]
    check("无关联签时其余 4 张仍在", left2 == 4, f"剩 {left2} 张")

    st.debug_log("query", "12345", "调试状态查询")
    st.debug_log("reset", "charX", "清了一次")
    qs = st.debug_queries(20)
    check("debug_queries 返回带 uid 的查询流水",
          len(qs) == 1 and qs[0]["target"] == "12345" and "created_at" in qs[0])
    check("debug_audit 不含 query 记录",
          all(r["kind"] != "query" for r in st.debug_audit(20)))
    st.close()

# ---------- ⑤ 查询历史（v0.50.0 改版：卡片下线，只留按钮右侧标签） ----------
check("调试页查询历史卡片已下线", "dbgQueries" not in PAGE
      and 'title="查询历史"' not in PAGE)
check("最近查询标签在「查询状态」按钮右侧且可直接查",
      'class="dbg-query-bar"' in PAGE and "最近查询" in PAGE
      and "pickRecentUser(u)" in PAGE)
check("进调试页 / 首屏 / 查完状态都会拉历史",
      "Vue.watch(menu, function (k)" in PAGE and PAGE.count("loadDebugHistory()") >= 4)
# ---------- ⑤b v0.50.0：头像 / 缩略图点开看大图 ----------
check("头像可点开大图（头像墙 / 资料页 / 图库页）",
      PAGE.count("openCharAvatarViewer") >= 4)
check("用户 QQ 头像可点开大图（总览 / 记录 / 用户列表 / 用户详情）",
      PAGE.count("openAvatarBig") >= 4)
# v0.51.0：最近抽签改「角色头像 + 名字点击进详情；卡面点开看大图」——
# 角色缩略不再承担「看大图」职责（openCharCoverViewer 已删）
check("总览最近抽签：角色头像/名字进详情、卡面看大图",
      "charAvatarOf(r.char_id)" in PAGE and 'gotoGallery(r.char_id)' in PAGE
      and "r.card_thumb" in PAGE and "openViewer(r.card_path" in PAGE)
check("记录表 / 用户详情的老婆相关列都带头像且可进详情",
      PAGE.count("charAvatarOf(") >= 10
      and PAGE.count("gotoGallery(d.char_id)") >= 1
      and PAGE.count("gotoGallery(h.char_id)") >= 1
      and PAGE.count("gotoGallery(c.char_id)") >= 1)
check("角色头像缺图兜底为文字头像（首字 + 应援色）",
      PAGE.count("char-av-letter") >= 4)
check("data URL 也能进 lightbox（不经 /image/data）",
      "function openImageViewerUrl" in PAGE and "lightbox.setSrc(src, name)" in PAGE)

# ---------- ⑥ 图生图默认源 ----------
check("自动选源优先级：出卡源 → 主参考 → 第一张",
      "function _defaultGenSource()" in PAGE
      and '{ path: galDrawRefAbs.value, label: "出卡图生图源" }' in PAGE
      and '{ path: galMainRef.value, label: "主参考图" }' in PAGE
      and 'return { path: src[0].path, label: "第一张可用图" };' in PAGE)
# v0.43.1 真机事故：立绘索引与参考图并行加载，立绘先到 → 自动选了 #1 立绘；
# 参考图到齐后因为「已有值就 return」永不纠正。四条守卫钉住修复。
check("自动选的源可被更优候选纠正（不被加载竞态锁死）",
      "galSrcAutoPicked" in PAGE
      and "var isAuto = cur.length === 1 && !!galSrcAutoPicked" in PAGE
      and "if (cur.length && !isAuto) { return; }" in PAGE)
check("用户手动选源后不再自动改写",
      'galSrcAutoPicked = "";\n        var arr = galRefsSel.value.slice();' in PAGE)
check("参考图加载完成后补一次自动选源",
      "Vue.watch(galRefsLoading, function (busy)" in PAGE)
check("路径按末尾归一比较（绝对/相对混存）",
      "function _samePath(a, b)" in PAGE
      and 'x.slice(-(y.length + 1)) === "/" + y' in PAGE)
check("生成源区显示自动选中依据",
      'v-if="galSrcHint"' in PAGE and "已自动选中：" in PAGE)
# v0.43.1：路径归一函数真跑（绝对/相对/反斜杠混存是这次的另一个坑）
import subprocess as _sp  # noqa: E402

_a = PAGE.index("function _samePath(a, b) {")
_b = PAGE.index("\n      }", _a) + len("\n      }")
_tmp_js = ROOT / "_tmp_samepath.js"
_tmp_js.write_text(PAGE[_a:_b] + r"""
var bad = 0;
function ck(name, cond) {
  console.log((cond ? "ok: " : "FAIL: ") + name);
  if (!cond) { bad++; }
}
var BS = String.fromCharCode(92);
ck("绝对 vs 相对同图", _samePath("X:/data/refs/a.png", "refs/a.png"));
ck("反斜杠归一",
   _samePath("x:" + BS + "data" + BS + "refs" + BS + "a.png", "refs/a.png"));
ck("大小写不敏感", _samePath("X:/Data/REFS/A.PNG", "refs/a.png"));
ck("完全相同", _samePath("refs/a.png", "refs/a.png"));
ck("不同图不误判", !_samePath("refs/a.png", "refs/b.png"));
ck("空值安全", !_samePath("", "refs/a.png") && !_samePath("refs/a.png", ""));
ck("仅 basename 相同不误判",
   !_samePath("X:/data/portraits/c1/main.png", "main.png"));
ck("同名不同目录不误判",
   !_samePath("X:/data/refs/a/x.png", "refs/b/x.png"));
process.exit(bad ? 1 : 0);
""", encoding="utf-8")
try:
    _r = _sp.run(["node", str(_tmp_js)], capture_output=True, text=True,
                 encoding="utf-8")
    _out = ((_r.stdout or "") + (_r.stderr or "")).strip()
    check("_samePath 真跑全过（node）", _r.returncode == 0, _out[-600:])
    print(_out)
finally:
    _tmp_js.unlink(missing_ok=True)
check("默认源在切模式 / 数据到位 / 开面板 / 提交四处兜底",
      PAGE.count("ensureDefaultGenSource();") >= 4)
check("openPromptModal 会先补默认源", "ensureDefaultGenSource();\n        galPromptOpen.value = true;" in PAGE)

# ---------- ⑦ 数量口径 / 合并页签 / 图库 ----------
check("速览与资料合并（overview 页签已移除）",
      'galTab.value = "overview"' not in PAGE
      and 'key: "overview", label' not in PAGE
      and 'galTab.value = "profile"' in PAGE)
check("新增图库页签", 'key: "library", label: "图库"' in PAGE
      and "galTab === 'library'" in PAGE)
check("图库汇总头像 / 参考图 / 立绘",
      all(k in PAGE for k in ("lib-avatar", "libref:", "libp:")))
check("所有配额展示改用 galActiveCount",
      "galActiveCount" in PAGE
      and "立绘 {{ galItems.length }}/" not in PAGE
      and '"立绘 " + galItems.value.length' not in PAGE)
check("停用数仍单独展示", "galDisabledCount" in PAGE)

# ---------- ⑧ 样式 / 固定 / 悬浮保存 / 头像弹窗 / 行尾 ----------
check("头像生成改走弹窗且只出候选",
      '@click="openAvatarGen"' in PAGE and "apply: false" in PAGE)
check("头像 busy 不在 finally 里提前释放（防重复点击）",
      "finally { galAvatarBusy.value = false; }" not in PAGE
      and "var finish = function ()" in PAGE)
check("头像结果可选替换 / 放弃", "applyAvatarResult" in PAGE
      and "discardAvatarResult" in PAGE)
check("页面级固定头部 + 轻量工具行固定",
      ".sticky-head" in PAGE and ".sticky-tools" in PAGE
      and PAGE.count("position: sticky") >= 3)
check("tab 换成分段下划线风格（不再是 999px 胶囊）",
      "border-radius: 10px 10px 0 0" in PAGE and "::after" in PAGE
      and ".gal-tab { padding: 5px 14px; border-radius: 999px" not in PAGE)
check("总览横幅 + 进度条", "ov-hero" in PAGE and "ovFullPct" in PAGE
      and "ov-quick-btn" in PAGE)
# v0.42.2 实测事故：.ov-card 的 height:100% 在默认 content-box 下会让
# 卡片比 grid 行高出 padding(28)+border(2)，父容器仍按行高排布 → 下一行
# 区块压在前一行卡片上（Chrome 实测 n-grid 120px / card 150px）。
check("总览卡片不再用 height:100%（防区块重叠复发）",
      ".ov-card { position: relative; box-sizing: border-box; overflow: hidden;" in PAGE
      and "padding: 14px 16px; height: 100%;" not in PAGE
      and "height: 100%;" not in PAGE.split(".ov-card {")[1].split("}")[0])
check("总览两个 grid 都用 ov-grid 让卡片等高且不溢出",
      PAGE.count('<n-grid class="ov-grid"') == 2
      and ".ov-grid > div { display: flex; }" in PAGE
      and ".ov-grid .ov-card { flex: 1 1 auto; width: 100%; }" in PAGE)
check("配置页分区锚点 + 悬浮保存条",
      "config-save-float" in PAGE and "cfg-sec-" in PAGE
      and "configDirty" in PAGE and "scrollToConfigSection" in PAGE)
check("index.html 全文件仍是 CRLF（v0.34.1 / v0.39.1 事故）",
      PAGE_BYTES.count(b"\n") > 7000
      and PAGE_BYTES.count(b"\r\n") == PAGE_BYTES.count(b"\n"))

# ---------- ⑨ v0.45.1：资料表单进页即回填 / 卡面一句话简介 ----------
# 事故：换角色时只清空 galProfile、等用户点「资料」页签才填，而页签本来
# 就是资料 → 进详情页看到空表单；印象语另有 watch 所以只有它显示（用户
# 实报「导入成功但只有印象语渲染出来，其他资料都是空的」）。
check("换角色时立刻回填资料表单（不等点页签）",
      "fillGalProfileFromEntry(true);" in PAGE)
check("资料表单跟随 detailEntry 同步（导入 / AI 落库后能看到）",
      "Vue.watch([galCharId, detailEntry], function () {" in PAGE
      and "fillGalProfileFromEntry();" in PAGE)
check("回填带内容指纹保护（不冲掉用户正在改的字）",
      "galProfileBaseline" in PAGE
      and "JSON.stringify(galProfile.value || {})" in PAGE)
check("卡面一句话简介字段（tagline）在资料表单里",
      'key: "tagline", label: "一句话简介"' in PAGE
      and "genGalTagline" in PAGE)
check("印象语给到 10 句量级（rows 6 + 实时句数）",
      "galQuoteCount" in PAGE and ':rows="6"' in PAGE)

# ---------- ⑩ v0.54.0：总览补全统计 / 今日抽签 / 记录页群与时间 / 头像 ----------
check("总览改「今日抽签」且时间到时分秒",
      "ov.today_recent" in PAGE and "fmtClock(r.updated_at)" in PAGE
      and "今日抽签" in PAGE and "ov.recent" not in PAGE)
check("今日抽签带群名 + 首抽/换签标注",
      "groupNameOf(r.group_id, r.group_name)" in PAGE
      and "rerollLabel(r.nonce)" in PAGE)
check("总览补全清单（立绘/头像/印象语）可点进角色详情",
      all(k in PAGE for k in ("ov.quota_short", "ov.no_avatar", "ov.no_quote"))
      and "gotoGallery(c.id)" in PAGE)
check("总览角色卡展示 总数 / 启用 / 停用",
      "ov.chars_enabled" in PAGE and "ov.chars_disabled" in PAGE
      and '"chars_disabled"' in API)
check("记录页新增抽卡时间列（时分秒，另有日期列）",
      "<th>抽卡时间</th>" in PAGE and "fmtClock(d.updated_at)" in PAGE
      and "<th>日期</th>" in PAGE)
check("记录页群列显示群名 + 群号（不再一律私聊）",
      "groupNameOf(d.group_id, d.group_name)" in PAGE
      and "groupTitle(d.group_id, d.group_name)" in PAGE)
check("记录页平台列走短名映射（不再露 PlatformMetadata repr）",
      "platformLabel(d.platform)" in PAGE and "PLATFORM_LABELS" in PAGE)
check("角色库默认头像墙，卡片第二、列表最后",
      'var charsMode = Vue.ref("avatar")' in PAGE
      and PAGE.index('<n-radio-button value="avatar">')
      < PAGE.index('<n-radio-button value="card">')
      < PAGE.index('<n-radio-button value="list">'))
check("帮助页有指令篇",
      'key: "commands"' in PAGE and "指令与玩法" in PAGE)
check("群头像只做 QQ 群图床 + 平台名归一",
      "p.qlogo.cn/gh/" in API and "def normalize_platform" in STORE
      and "get_platform_name" in MAIN)
check("脏平台名在启动时幂等清洗",
      "_normalize_platforms" in STORE and "PlatformMetadata%" in STORE)

# ---------- ⑪ v0.54.2：私聊开关 / 统计口径 / 卡面不快照 ----------
_SCHEMA = (ROOT / "_conf_schema.json").read_text(encoding="utf-8")
check("私聊抽卡开关默认关闭且两个指令都拦",
      '"allow_private_draw"' in _SCHEMA
      and MAIN.count("if not gid and not self._allow_private()") == 2)
check("卡面统计口径可配（all / group）",
      '"same_stat_scope"' in _SCHEMA and "same_stat_scope" in MAIN
      and "same_stat_scope" in PAGE)
check("卡面统计不再是一次性快照（重发前比对 + 变了才重渲染）",
      "stale = (" in MAIN and "today_char_stats" in MAIN
      and "today_rank=rank" in MAIN and "update_draw_card(uid, date, card_path, same_count=same" in MAIN)
check("卡面新增「今日第几个抽到她」且宽度自适应",
      "today_rank" in (ROOT / "card.py").read_text(encoding="utf-8")
      and "个抽到她" in (ROOT / "card.py").read_text(encoding="utf-8"))
check("统计快照列有迁移（老库就地补列）",
      '"same_count"' in STORE and '"today_rank"' in STORE and '"total_count"' in STORE)

# ---------- ⑫ v0.54.3：头像图床换端点 + 群名补全 ----------
check("用户头像不再用已失效的 q4 端点",
      "q.qlogo.cn/headimg_dl?dst_uin=" in API
      and 'https://q4.qlogo.cn' not in API)
check("头像/群图拉取带 UA 且失败静默",
      "_http_image_data_url" in API and "User-Agent" in API)
check("群名可从适配器补全并写回库",
      "/groups/names" in API and "h_groups_names" in API
      and "set_group_name" in STORE and "ensureGroupNames" in PAGE)
check("抽卡时群名失败原因进日志（warning，不再 debug 吞掉）",
      "群名获取失败" in MAIN and "call_action(\"get_group_info\"" in MAIN)

if FAILURES:
    print(f"test_ui_v042: {len(FAILURES)} FAILED")
    sys.exit(1)
print("test_ui_v042: ALL OK")
