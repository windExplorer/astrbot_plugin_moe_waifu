# -*- coding: utf-8 -*-
"""全局参考图库 + 角色替换（v0.56.0）契约守卫，可直接运行，退出码即结论。

盯住最容易「改一处漏一处」的四条线：
  1) 后端四条路由 + store 表/方法齐全；
  2) 前端两处 swap 模式都把 refs 拼成 [全局, 角色]（顺序=画布/身份，反了功能全废）；
  3) 固定 Qwen 双图编辑指令用自然语言「图1/图2」引用（v0.71.4 起，弃 <image1> 标记），
     且 raw_prompt 直通不被 prompt_gen 覆盖；
  4) 菜单 / 上传端点新参数 / 两段删除确认都落地。
"""
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "pages" / "waifu-console" / "index.html"
API = ROOT / "webui_api.py"
STORE = ROOT / "store.py"

FAILURES = []


def check(name, cond, detail=""):
    if not cond:
        FAILURES.append(name)
        print(f"FAIL: {name} {detail}")
    else:
        print(f"ok: {name}")


def main():
    sys.path.insert(0, str(ROOT))
    page = PAGE.read_text(encoding="utf-8")
    api = API.read_text(encoding="utf-8")
    store = STORE.read_text(encoding="utf-8")

    # 1) 后端：四条路由 + 表 + 方法
    for r in ("global_refs/list", "global_refs/upload", "global_refs/note",
              "global_refs/delete"):
        check(f"路由已注册 {r}", f'"/{r}"' in api)
    check("global_refs 表在 SCHEMA", "CREATE TABLE IF NOT EXISTS global_refs" in store)
    for m in ("add_global_ref", "list_global_refs", "get_global_ref",
              "set_global_ref_note", "delete_global_ref"):
        check(f"store 方法存在 {m}", f"def {m}(" in store)

    # v0.64.0：高清封面 + 列数偏好（后端产出字段 / 前端接入）
    check("全局图库返回 1000px 高清 cover", '"cover": _thumb_cached(fp, 1000)' in api)
    check("ui_state GET 回传 gr_cols", '"gr_cols": gr_cols' in api)
    check("ui_state POST 只改列数时 menu 可省",
          '"gr_cols" not in body' in api and 'data["gr_cols"]' in api)
    check("列数范围钳在 2~8（越界归 0=自适应）",
          "2 <= cols <= 8" in api and "2 <= v <= 8" in api)
    check("前端封面用高清 cover 回退 thumb", ':src="r.cover || r.thumb"' in page)
    check("前端列数下拉挂载并暴露",
          'v-model:value="grColsSel"' in page and "grColsSel: grColsSel" in page)

    # 2) 两处 swap 的 refs 顺序：[全局, 角色]，绝不能颠倒
    #    试画台 overrideOf 的 swap 分支
    ov = re.search(r'tryMode\.value === "swap"\) \{\s*var ov0 =.*?return ov0;', page, re.S)
    check("试画台 swap override 分支存在", ov is not None)
    if ov:
        blk = ov.group(0)
        refs = re.search(r"refs:\s*\[\s*([^\]]+)\]", blk)
        check("试画台 swap refs 顺序=[全局,角色]",
              bool(refs) and "trySwapGlobal" in refs.group(1)
              and refs.group(1).index("trySwapGlobal") < refs.group(1).index("trySwapChar"),
              refs.group(1) if refs else "")
        check("试画台 swap 强制 img2img", "strategy_type: \"img2img\"" in blk)
    #    角色详情 doDetailGenerate 的 swap 分支
    ov2 = re.search(r'galMode\.value === "swap"\) \{\s*// v0\.56.*?payload\.override = ov2;', page, re.S)
    check("角色详情 swap override 分支存在", ov2 is not None)
    if ov2:
        blk = ov2.group(0)
        refs = re.search(r"refs:\s*\[\s*([^\]]+)\]", blk)
        check("角色详情 swap refs 顺序=[全局,角色]",
              bool(refs) and "galSwapGlobal" in refs.group(1)
              and refs.group(1).index("galSwapGlobal") < refs.group(1).index("galSwapChar"),
              refs.group(1) if refs else "")
        check("角色详情 swap 强制 img2img", "strategy_type: \"img2img\"" in blk)
    # v0.60.2：Qwen Edit 双图编辑不支持 denoise——两处 swap override 都不能带它，
    # 否则传一个被忽略的无效参数，界面还摆控件误导用户去调。
    check("角色详情 swap override 不注入 denoise", "galDenoise.value" not in blk, blk)

    ov_try = re.search(r'if \(tryMode\.value === "swap"\) \{.*?return ov0;', page, re.S)
    ov_try_blk = ov_try.group(0) if ov_try else ""
    check("试画台 swap override 不注入 denoise",
          bool(ov_try_blk) and "trySwapDenoise.value" not in ov_try_blk, ov_try_blk[:120])
    check("swap 模式不显示重绘强度控件",
          'v-if="galMode === \'img2img\' || galMode === \'swap\'" v-model:value="galDenoise"' not in page
          and 'v-model:value="trySwapDenoise"' not in page)

    # 3) 固定编辑指令：双图自然语言引用「图1/图2」（v0.71.4 弃 <image1> 标记），两处都用同一常量
    check("编辑指令用 图1 画布 + 图2 身份（不再用 <imageN> 标记）",
          "图1" in page and "图2" in page
          and "<image1>" not in page and "<image2>" not in page
          and "画布" in page and "身份" in page)
    # v0.61.0 三档 → v0.76.0 六档（新增 hair / body / outfit），
    # 默认仍是 appearance。提示词全部换成真机验证的 C 框架：动作先行 + 双向锚定。
    sw = re.search(r'var SWAP_SCHEMES = \[(.*?)\n  \];', page, re.S)
    schemes = sw.group(1) if sw else ""
    check("六档替换方案齐全（face/appearance/full/hair/body/outfit）",
          all('key: "%s"' % k in schemes for k in
              ("face", "appearance", "full", "hair", "body", "outfit")), schemes[:120])
    check("标准档含体型/发型/头顶饰物跟随图2",
          "体型身材" in schemes and "发型发色" in schemes
          and "胸部的大小与丰满程度" in schemes, schemes[:120])
    check("所有档都用 C 框架（动作先行 + 双向锚定，无旧版否定句）",
          schemes.count("唯一的变化是") == 6
          and "图1 是画布：保留" not in schemes
          and "不要带 图2 的服装、动作或表情" not in schemes, schemes[:120])
    check("新增档：发型 / 体型 / 穿搭",
          "只换发型发色与头饰" in schemes and "只换身材比例" in schemes
          and "只换衣服配饰" in schemes)
    check("默认方案是标准档 appearance",
          'Vue.ref("appearance")' in page)
    check("按 key 取指令的函数存在且回退标准档",
          "function swapSchemePrompt(key)" in page
          and "return SWAP_SCHEMES[1].prompt" in page)
    check("不得再有「表情跟随 image2」的错误措辞（v0.60.1）",
          "表情与神态同样跟随" not in page
          and "不要用 <image1> 的表情" not in page
          and "除人物身份与表情外" not in page)
    check("两处面板都有方案切换下拉",
          page.count(':options="swapSchemeOpts"') >= 2)
    check("换方案会同步刷新两处提示词",
          "Vue.watch(swapScheme, function ()" in page
          and "tryPrompt.value = trySwapPrompt()" in page
          and "swapPromptWithHair(swapSchemePrompt(swapScheme.value)" in page)
    check("方案状态已暴露给模板",
          'swapScheme: swapScheme, swapSchemeOpts: swapSchemeOpts, swapSchemeHint: swapSchemeHint' in page)
    check("试画台 swap 提示词走方案函数",
          "function trySwapPrompt() {" in page
          and "swapPromptWithHair(swapSchemePrompt(swapScheme.value),\n                                  tryEntry.value)" in page)
    check("角色详情 swap 提示词走方案函数",
          page.count("swapPromptWithHair(swapSchemePrompt(swapScheme.value),") >= 2
          and "swapPromptWithHair(swapSchemePrompt(swapScheme.value),\n                                               detailEntry.value)" in page)
    check("swap 按角色注入发色备注（v0.71.9 hair_note）",
          "function swapPromptWithHair(base, e)" in page
          and "e.hair_note" in page
          and "不得把头发涂成单一发色" in page)

    # 4) 菜单 / 上传端点 / 两段删除
    check("前端新增全局图库页签", 'key: "global_refs"' in page)
    check("后端页签白名单含 global_refs", '"global_refs"' in api)
    check("apiUpload 支持自定义端点",
          "async function apiUpload(file, endpoint)" in page
          and 'bridge.upload(ep, file)' in page)
    check("全局图库上传端点正确", '"global_refs/upload"' in page)
    check("删除走两段确认（服务端）", "body.get(\"confirm\")" in api
          and "h_global_refs_delete" in api)

    # 5) store 真库 CRUD
    try:
        import store as store_mod
        with tempfile.TemporaryDirectory() as td:
            s = store_mod.Store(Path(td) / "t.db")
            s.add_global_ref("global_refs/g_1.png", "h1", 10, 20, note="画布")
            s.add_global_ref("global_refs/g_2.png", "h2")
            check("真库：新增后 2 行", len(s.list_global_refs()) == 2)
            check("真库：备注可读", s.get_global_ref("global_refs/g_1.png")["note"] == "画布")
            s.set_global_ref_note("global_refs/g_1.png", "改过")
            check("真库：备注可改", s.get_global_ref("global_refs/g_1.png")["note"] == "改过")
            check("真库：删除生效", s.delete_global_ref("global_refs/g_2.png")
                  and len(s.list_global_refs()) == 1)
            s.close()  # Windows 下不关连接会导致临时目录清理报 WinError 32
    except Exception as exc:  # pragma: no cover
        check("store 真库 CRUD 无异常", False, f"{exc.__class__.__name__}: {exc}")

    print("test_global_refs_swap: " + ("ALL OK" if not FAILURES else f"{len(FAILURES)} FAILED"))
    sys.exit(0 if not FAILURES else 1)


if __name__ == "__main__":
    main()
