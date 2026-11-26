# -*- coding: utf-8 -*-
"""衣柜 / 角色试穿（v0.72.0）契约守卫，可直接运行，退出码即结论。

盯住四条线：
  1) 后端六个路由 + wardrobe 表 / store 五个方法齐全；
  2) 前端出图 refs 顺序固定 [角色参考图, 衣服...]，强制 img2img，不传 denoise；
  3) 编辑指令口径：图1 锁身份 / 图2 起是衣服，缺上衣或鞋时「扩写搭配」，
     三视图锁多视角排布；纯函数可直接断言；
  4) 真库 CRUD + 两段删除 + 页签 / 上传端点落地。
"""
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "pages" / "waifu-console" / "index.html"
API = ROOT / "webui_api.py"
STORE = ROOT / "store.py"
PROMPT = ROOT / "prompt_gen.py"

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
    prompt_src = PROMPT.read_text(encoding="utf-8")

    # 1) 后端：六条路由 + 表 + 方法
    for r in ("wardrobe/list", "wardrobe/upload", "wardrobe/update",
              "wardrobe/delete", "wardrobe/prompt", "wardrobe/expand"):
        check(f"路由已注册 {r}", f'"/{r}"' in api)
    check("wardrobe 表在 SCHEMA", "CREATE TABLE IF NOT EXISTS wardrobe" in store)
    for m in ("add_wardrobe", "list_wardrobe", "get_wardrobe",
              "set_wardrobe_meta", "delete_wardrobe"):
        check(f"store 方法存在 {m}", f"def {m}(" in store)
    check("上传走 sha16 内容寻址", "c_{digest}{suffix}" in api)
    check("删除走回收站（不 unlink）", "_trash_files(plugin, [rel]" in api)

    # 2) 前端出图：refs = [角色, 衣服...]，强制 img2img，不注入 denoise
    gen = re.search(r"async function wardGenerate\(\).*?\n      \}", page, re.S)
    check("wardGenerate 存在", gen is not None)
    blk = gen.group(0) if gen else ""
    check("试穿 refs 顺序=[角色参考图, 衣服...]",
          "var refs = [wardCharRef.value].concat(wardClothes.value)" in blk, blk[:160])
    check("试穿强制 img2img", 'strategy_type: "img2img"' in blk)
    check("试穿不注入 denoise（Qwen Edit 不支持）", "denoise" not in blk)
    check("试穿提示词走 jobs/start 的 manual 直通",
          'apiPost("jobs/start"' in blk and "prompt:" in blk)
    check("衣服最多 4 件（多图工作流上限）", ':max="4"' in page)

    # 3) 编辑指令纯函数
    try:
        import prompt_gen as pg
        check("只有裙子：缺上衣与鞋履",
              pg.wardrobe_missing_parts(["skirt"]) == ["上衣", "鞋履"],
              str(pg.wardrobe_missing_parts(["skirt"])))
        check("只有裤子：缺上衣与鞋履",
              pg.wardrobe_missing_parts(["bottom"]) == ["上衣", "鞋履"])
        check("连衣裙自带上下装，只缺鞋履",
              pg.wardrobe_missing_parts(["dress"]) == ["鞋履"])
        check("上衣+下装+鞋齐全：无缺失项",
              pg.wardrobe_missing_parts(["top", "bottom", "shoes"]) == [])
        p = pg.wardrobe_edit_prompt(["skirt"], "白色蕾丝短裙", "complete")
        check("扩写只发生在缺失部位",
              "没有展示上衣、鞋履" in p and "只在缺失的上衣、鞋履上做补充" in p, p)
        check("图1 锁身份、图2 是必须照穿的衣服",
              "图1 是角色参考图" in p and "严格穿上 图2 中的" in p)
        # v0.73.2：生成衣服与图片完全不一样的两个根因，守卫锁死不再复发
        check("图2 衣服是硬约束（不得重新设计）",
              "必须原样照穿" in p and "不得重新设计" in p, p)
        check("旧衣服指代明确（消除「原服装」歧义）",
              "图1 角色原本穿着的服装要完全换掉" in p
              and "原服装的款式与轮廓不要残留" not in p, p)
        p_outfit = pg.wardrobe_edit_prompt(["skirt"], "", "complete", "米色针织衫与乐福鞋")
        check("AI 扩写文本降级为补充建议（冲突以图2 为准）",
              "补充搭配建议（只用于 图2 没展示的部位；与 图2 冲突时以 图2 为准）"
              in p_outfit and "完整穿搭：" not in p_outfit, p_outfit)
        check("AI 扩写后端只写缺失部位、不重写整套",
              "只输出缺失部位的搭配建议" in prompt_src
              and "不要描述或改动已有的那件衣服" in prompt_src)
        p_only = pg.wardrobe_edit_prompt(["skirt"], "", "only")
        check("只换方案不扩写", "不扩写、不新增衣物" in p_only and "只在缺失的" not in p_only)
        p_view = pg.wardrobe_edit_prompt(["skirt"], "", "complete", "", True)
        check("三视图输入 → 单人立绘输出",
              "多视角设定稿" in p_view and "只输出一张单人全身立绘" in p_view
              and "不要复刻多视角并列排版" in p_view, p_view)
        # draw.detect_view_layout 见到这些词会把图生图尺寸切成横/方形；
        # 我们要竖版单人照，指令里绝不能出现它们。
        check("指令避开视图尺寸检测词（否则被切成横版）",
              not any(k in p_view for k in ("三视图", "四视图", "multiple views", "four views")),
              p_view)
        check("前端显式钉住竖版尺寸",
              "width: wardSize.value.width, height: wardSize.value.height" in blk)
        check("后端按配置下发出图尺寸",
              '"width": int(cfg.get("draw_width"' in api
              and '"height": int(cfg.get("draw_height"' in api)
        check("后端 prompt 端点复用同一纯函数",
              "prompt_gen.wardrobe_edit_prompt(" in api)
        check("AI 扩写落回前端提示词框",
              "wardOutfitText.value = (res && res.text)" in page
              and "outfit_text: wardOutfitText.value" in page)
    except Exception as exc:  # pragma: no cover
        check("prompt_gen 纯函数无异常", False, f"{exc.__class__.__name__}: {exc}")

    # 4) 菜单 / 上传端点 / 两段删除
    check("前端新增衣柜页签", 'key: "wardrobe"' in page)
    check("后端页签白名单含 wardrobe", '"wardrobe"' in api)
    check("衣柜上传端点正确", '"wardrobe/upload"' in page)
    check("修改衣物信息端点落地", '"wardrobe/update"' in page)
    check("删除需服务端 confirm", 'body.get("confirm")' in api
          and "h_wardrobe_delete" in api)
    check("前端两段删除确认态", "wardDeleteArm === r.path" in page
          and "再点一次确认删除" in page)

    # 5) store 真库 CRUD
    try:
        import store as store_mod
        with tempfile.TemporaryDirectory() as td:
            s = store_mod.Store(Path(td) / "t.db")
            s.add_wardrobe("wardrobe/c_1.png", "h1", 10, 20,
                           name="白裙", category="skirt", note="蕾丝")
            s.add_wardrobe("wardrobe/c_2.png", "h2")
            check("真库：新增后 2 件", len(s.list_wardrobe()) == 2)
            row = s.get_wardrobe("wardrobe/c_1.png")
            check("真库：类型与备注可读",
                  row["category"] == "skirt" and row["note"] == "蕾丝", str(row))
            s.set_wardrobe_meta("wardrobe/c_1.png", "蓝裙", "dress", "改过")
            row = s.get_wardrobe("wardrobe/c_1.png")
            check("真库：元数据可改", row["name"] == "蓝裙"
                  and row["category"] == "dress" and row["note"] == "改过")
            check("真库：删除生效", s.delete_wardrobe("wardrobe/c_2.png")
                  and len(s.list_wardrobe()) == 1)
            s.close()  # Windows 下不关连接会导致临时目录清理报 WinError 32
    except Exception as exc:  # pragma: no cover
        check("store 真库 CRUD 无异常", False, f"{exc.__class__.__name__}: {exc}")

    print("test_wardrobe: " + ("ALL OK" if not FAILURES else f"{len(FAILURES)} FAILED"))
    sys.exit(0 if not FAILURES else 1)


if __name__ == "__main__":
    main()
