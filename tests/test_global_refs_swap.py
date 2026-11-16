# -*- coding: utf-8 -*-
"""全局参考图库 + 角色替换（v0.56.0）契约守卫，可直接运行，退出码即结论。

盯住最容易「改一处漏一处」的四条线：
  1) 后端四条路由 + store 表/方法齐全；
  2) 前端两处 swap 模式都把 refs 拼成 [全局, 角色]（顺序=画布/身份，反了功能全废）；
  3) 固定 Qwen 双图编辑指令用 <image1>/<image2>，且 raw_prompt 直通不被 prompt_gen 覆盖；
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

    # 3) 固定编辑指令：Qwen 双图标签，两处都用同一常量
    check("编辑指令用 <image1> 画布 + <image2> 身份",
          "<image1>" in page and "<image2>" in page
          and "画布" in page and "身份" in page)
    # v0.60.1：表情/神态必须跟随 <image2>。v0.56 原指令写「除人物身份外不做任何
    # 其它改动」+「姿势一律不改」，模型把表情归进画布侧保留 → 换脸不换表情。
    swap_inst = re.search(r'var SWAP_INSTRUCTION = "(.*?)";', page, re.S)
    inst = swap_inst.group(1) if swap_inst else ""
    check("编辑指令含表情跟随 image2 的明确措辞",
          "表情" in inst and "神态" in inst and "<image2>" in inst
          and "不要用 <image1> 的表情" in inst, inst)
    check("编辑指令不用「除人物身份外不做任何其它改动」压制表情",
          "除人物身份外不做任何其它改动" not in inst
          and "除人物身份与表情外" in inst, inst)
    check("试画台 swap 提示词走固定指令",
          "tryPrompt.value = trySwapPrompt()" in page)
    check("角色详情 swap 提示词走固定指令",
          "galPrompt.value = SWAP_INSTRUCTION" in page)

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
