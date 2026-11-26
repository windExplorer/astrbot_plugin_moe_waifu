# -*- coding: utf-8 -*-
"""角色详情「试穿」页签 + 试穿成果图库（v0.73.0）契约守卫，可直接运行。

盯住五条线：
  1) 后端五条路由 + tryon_works 表 / store 六方法；
  2) galTabs 新增 tryon 且原 library 图库保留，两处都能看到成果；
  3) 出图 refs=[角色参考图, 衣服...]、kind="try"（不自动归档），成功后存图库；
  4) 不自动进立绘、手动「设为立绘」走 /tryon/to_portrait，删除走回收站；
  5) 真库 CRUD（含内容寻址去重与转立绘标记）。
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

    # 1) 后端：路由 / 表 / 方法
    for r in ("tryon/list", "tryon/save", "tryon/update", "tryon/delete",
              "tryon/to_portrait"):
        check(f"路由已注册 {r}", f'"/{r}"' in api)
    check("tryon_works 表在 SCHEMA", "CREATE TABLE IF NOT EXISTS tryon_works" in store)
    for m in ("add_tryon", "list_tryons", "get_tryon", "set_tryon_meta",
              "mark_tryon_portrait", "delete_tryon"):
        check(f"store 方法存在 {m}", f"def {m}(" in store)

    # 2) 角色详情页签
    check("试穿页签已挂上（参考图之后、图库之前）",
          '{ key: "tryon", label: "试穿 "' in page
          and page.index('{ key: "refs", label: "参考图 "')
          < page.index('{ key: "tryon", label: "试穿 "')
          < page.index('{ key: "library", label: "图库" }'))
    check("试穿页签模板存在", "galTab === 'tryon'" in page)
    check("原图库页签保留且汇总试穿成果",
          "galTab === 'library'" in page and "试穿成果" in page
          and "libtryon:" in page)
    check("成果按分类分组展示", "galTryonGroups" in page and "grp.label" in page)
    # v0.73.1：w-pick-thumb 的图序徽章曾误用横向条带的 .picker-strip-ord，
    # 在纵向 pick-thumb-txt 里被 stretch 成全宽粉条；专用 class 锁住不再复发。
    check("图序徽章用专用 class（防粉色横条复发）",
          'class: "pick-thumb-ord"' in page and ".pick-thumb-ord {" in page
          and '"picker-strip-ord" }, String(this.index)' not in page)
    check("哈希文件名显示未命名衣物", "clothDisplayName" in page
          and "未命名衣物" in page)

    # 3) 出图链路：refs 顺序 + kind=try + 成功即存图库
    gen = re.search(r"async function galTryonGenerate\(\).*?\n      \}", page, re.S)
    check("galTryonGenerate 存在", gen is not None)
    gblk = gen.group(0) if gen else ""
    check("试穿 refs 顺序=[角色参考图, 衣服...]",
          "var refs = [galTryonCharRef.value].concat(galTryonClothes.value)" in gblk,
          gblk[:160])
    check("kind=try（_run_job 天然不归档立绘）", 'kind: "try"' in gblk)
    check("强制 img2img 且显式尺寸",
          'strategy_type: "img2img"' in gblk
          and "width: galTryonSize.value.width" in gblk)
    check("不注入 denoise（Qwen Edit 不支持）", "denoise" not in gblk)
    poll = re.search(r"function galTryonPoll\(\).*?\n      \}", page, re.S)
    pblk = poll.group(0) if poll else ""
    check("出图成功后存进试穿图库",
          'apiPost("tryon/save"' in pblk and "categories: cats" in pblk, pblk[:160])
    check("存库提示明示未进立绘", "未进立绘" in pblk)

    # 4) 手动转立绘 / 删除
    check("设为立绘走 /tryon/to_portrait",
          'apiPost("tryon/to_portrait"' in page
          and "draw.archive_portrait" in api
          and 'add_portrait(\n        cid, archived' in api)
    check("转立绘后标记 portrait_file（不重复转）",
          "mark_tryon_portrait(rid, archived)" in api
          and 'row.get("portrait_file")' in api)
    check("删除需两段确认 + 回收站",
          "galTryonDeleteArm === r.id" in page
          and "h_tryon_delete" in api and "body.get(\"confirm\")" in api
          and "_trash_files(plugin, [rel]" in api)

    # 5) 真库 CRUD
    try:
        import store as store_mod
        with tempfile.TemporaryDirectory() as td:
            s = store_mod.Store(Path(td) / "t.db")
            rid = s.add_tryon("c1", "tryons/t_1.png", "h1", "skirt", "白裙",
                              "prompt", 700, 1200, 42)
            check("真库：新增拿到行 id", rid > 0)
            check("真库：按角色列出", len(s.list_tryons("c1")) == 1
                  and len(s.list_tryons("c2")) == 0)
            # 同 file 重复登记覆盖元数据而非插新行（内容寻址去重）
            rid2 = s.add_tryon("c1", "tryons/t_1.png", "h1", "dress", "蓝裙")
            check("真库：同文件去重覆盖", rid2 == rid
                  and len(s.list_tryons("c1")) == 1)
            row = s.get_tryon(rid)
            check("真库：分类 / 名称可读",
                  row["category"] == "dress" and row["name"] == "蓝裙", str(row))
            s.set_tryon_meta(rid, "set", "套装甲")
            check("真库：分类可改", s.get_tryon(rid)["category"] == "set")
            s.mark_tryon_portrait(rid, "portraits/c1/x.png")
            check("真库：立绘标记可写",
                  s.get_tryon(rid)["portrait_file"] == "portraits/c1/x.png")
            check("真库：删除生效", s.delete_tryon(rid)
                  and len(s.list_tryons("c1")) == 0)
            s.close()  # Windows 下不关连接会导致临时目录清理报 WinError 32
    except Exception as exc:  # pragma: no cover
        check("store 真库 CRUD 无异常", False, f"{exc.__class__.__name__}: {exc}")

    print("test_tryon_gallery: " + ("ALL OK" if not FAILURES else f"{len(FAILURES)} FAILED"))
    sys.exit(0 if not FAILURES else 1)


if __name__ == "__main__":
    main()
