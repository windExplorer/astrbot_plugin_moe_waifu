# -*- coding: utf-8 -*-
"""「删除立绘图片」全局安全守卫（直接运行，退出码即结论）。

背景（v0.42.0 数据事故）：调试页点「重置今日抽卡记录」把某角色当天十几张
立绘一次删光。修完那一处后做全仓审计，发现还有几处同类隐患：
  ① 日常操作「立绘转参考图」是**硬 unlink**，不进回收站（反悔找不回）；
  ② 批量清空 / 重试当日生成 虽进回收站但**没写 _meta.json**，还原只靠
     文件名猜，prompt/theme/dims/停用态全丢（「找得回文件、找不回数据」）；
  ③ `reset_char_day` 仍按 (char_id,date) 批量清卡路径，制造一片死链；
  ④ `/refs/unbind` 能硬删任意 data/ 内文件，含 portraits/，且行还在；
  ⑤ `/trash/purge` 漏传 stamps 就全清，摧毁全部可恢复性。

这个守卫同时盯「代码形状」（静态）和「真跑一遍 SQLite + 回收站」，
任何一条路径退回旧写法都会立刻失败。
"""
import ast
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import store as store_mod  # noqa: E402
import webui_api as api  # noqa: E402
from PIL import Image  # noqa: E402

PAGE_PATH = ROOT / "pages" / "waifu-console" / "index.html"
PAGE = PAGE_PATH.read_text(encoding="utf-8")
PAGE_BYTES = PAGE_PATH.read_bytes()
API = (ROOT / "webui_api.py").read_text(encoding="utf-8")
MAIN = (ROOT / "main.py").read_text(encoding="utf-8")
STORE = (ROOT / "store.py").read_text(encoding="utf-8")

FAILURES = []


def check(name, cond, detail=""):
    if not cond:
        FAILURES.append(name)
        print(f"FAIL: {name} {detail}")
    else:
        print(f"ok: {name}")


def func_src(src, name):
    """按 AST 抠出函数/方法源码（store.py 的方法在 class 里，必须 ast.walk）。"""
    lines = src.splitlines()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    return ""


# ---------- 1. 静态：硬删除必须绝迹 ----------
api_unlinks = [(i + 1, l.strip()) for i, l in enumerate(API.splitlines())
               if ".unlink(" in l]
check("webui_api.py 只剩一处硬 unlink（头像候选图）",
      len(api_unlinks) == 1, str(api_unlinks))
check("唯一硬 unlink 位于 /avatar/discard（只清 _pending_ 候选）",
      ".unlink(" in func_src(API, "h_avatar_discard")
      and "_pending_" in func_src(API, "h_avatar_discard"))
for fn in ("_unlink_portrait", "_delete_portrait_core", "_delete_card_files",
           "h_refs_unbind", "h_refs_orphan_delete"):
    body = func_src(API, fn)
    check(f"{fn} 不再硬 unlink", bool(body) and ".unlink(" not in body)
check("main.py 的两处 unlink 只清理 cards/ 与 try_cache/（不碰 portraits）",
      MAIN.count(".unlink(") == 2
      and ".unlink(" in func_src(MAIN, "_prune_cards")
      and ".unlink(" in func_src(MAIN, "_prune_try_cache"))

# ---------- 2. 静态：删立绘一律带完整 meta + 按主键删行 ----------
unlink_body = func_src(API, "_unlink_portrait")
check("立绘转参考图走回收站且带整行 meta",
      "_trash_files(" in unlink_body and "meta=" in unlink_body
      and "portrait_by_file" in unlink_body)
check("delete_portrait 支持按主键删（最多一行）",
      "def delete_portrait(self, file: str, row_id" in STORE
      and "DELETE FROM portraits WHERE id=?" in func_src(STORE, "delete_portrait"))
check("所有 delete_portrait 调用都传 row_id",
      API.count("delete_portrait(") == API.count("row_id=row.get("),
      f"调用 {API.count('delete_portrait(')} 处 / 传 row_id {API.count('row_id=row.get(')} 处")

clear_body = func_src(API, "h_debug_clear_portraits")
check("批量清空立绘库：先抓完整行再删、并写 meta",
      "char_portrait_rows(cid)" in clear_body
      and "meta=meta" in clear_body
      and clear_body.index("char_portrait_rows") < clear_body.index("clear_char_portraits"))
reset_body = func_src(API, "h_debug_reset_char_day")
check("重试当日生成写完整行 meta", 'meta=res.get("removed_rows")' in reset_body)
check("store.reset_char_day 返回 removed_rows",
      '"removed_rows": removed_rows' in func_src(STORE, "reset_char_day"))
check("reset_char_day 的卡路径更新收窄到关联签（不再按日期批量清）",
      "AND portrait_file=?" in func_src(STORE, "reset_char_day")
      and "if target:" in func_src(STORE, "reset_char_day"))
check("clear_char_portraits 是唯一保留的宽条件批量删（注释写明三重约束）",
      STORE.count("DELETE FROM portraits WHERE char_id=?") == 1
      and "char_portrait_rows()" in func_src(STORE, "clear_char_portraits"))

# ---------- 3. 静态：其它入口的护栏 ----------
check("/refs/unbind 拒绝硬删 portraits/ 且 refs 删除走回收站",
      'rel_s.startswith("portraits/")' in func_src(API, "h_refs_unbind")
      and "_trash_files(" in func_src(API, "h_refs_unbind"))
check("/trash/purge 全清需显式 purge_all",
      'body.get("purge_all")' in func_src(API, "h_trash_purge")
      and "purge_all: true" in PAGE)
check("批量去重补两段确认", 'armDelete("dedupe")' in PAGE)
check("按角色清空立绘库的确认提示带张数与可还原说明",
      "portraitCount(cid)" in PAGE and "文件进回收站可还原" in PAGE)
check("两处清库入口不再同名覆盖（各自绑定、各自 armed key）",
      PAGE.count("function doClearCharPortraits(") == 1
      and "function doClearCharPortraitsQuick()" in PAGE
      and '@click="doClearCharPortraitsQuick"' in PAGE
      and '@click="doClearCharPortraits"' in PAGE
      and 'return _clearCharPortraits(row.char_id' in PAGE
      and 'return _clearCharPortraits(dbgChar2.value' in PAGE)
check("index.html 仍是全文件 CRLF",
      PAGE_BYTES.count(b"\n") > 7000
      and PAGE_BYTES.count(b"\r\n") == PAGE_BYTES.count(b"\n"))

# ---------- 4. 真跑：SQLite + 回收站 + 元数据 ----------
DATE = "2026-11-14"
CID = "charX"


class Plugin:
    def __init__(self, data_dir, st):
        self._data_dir = str(data_dir)
        self._store = st
        self.context = object()

    def _today(self):
        return DATE


with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    st = store_mod.Store(td / "waifu.db")
    files = []
    for i in range(1, 7):
        rel = f"portraits/{CID}/{DATE}_{i:03d}_{i:05d}.png"
        fp = td / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8), (i * 20 % 255, 10, 10)).save(fp)
        st.add_portrait(CID, rel, 1000 + i, DATE, i, "base",
                        prompt=f"prompt-{i}", theme=f"theme-{i}",
                        dims="[[a,b]]", width=8, height=8)
        files.append(rel)
    st.set_portrait_active(files[4], False)
    plugin = Plugin(td, st)

    rows = st.char_portrait_rows(CID)
    check("char_portrait_rows 抓到全部 6 行且含停用态 / 提示词",
          len(rows) == 6
          and any(r["file"] == files[4] and int(r["active"]) == 0 for r in rows)
          and any(r["prompt"] == "prompt-3" for r in rows))

    # 两条当天签：u1 用第 3 张立绘、u2 用第 1 张，卡图路径都非空
    for uid, pf in (("u1", files[2]), ("u2", files[0])):
        st.upsert_draw(uid, DATE, CID, 0, False, "nick", "g1", "群", "aiocqhttp",
                       seed_hex="", char_name="X", series="", portrait_file=pf,
                       decision="generate")
        st._conn.execute("UPDATE draws SET card_path=? WHERE user_id=?",
                         (f"cards/{uid}_{DATE}.png", uid))
    st._conn.commit()

    res = st.reset_char_day(CID, DATE)
    check("reset 只删一张并返回该行完整快照",
          res["removed_files"] == [files[2]]
          and list(res["removed_rows"]) == [files[2]]
          and res["removed_rows"][files[2]]["prompt"] == "prompt-3"
          and "active" in res["removed_rows"][files[2]])
    u2 = st._conn.execute("SELECT card_path FROM draws WHERE user_id='u2'").fetchone()
    check("reset 不再批量清其它用户的卡路径（无死链）",
          str(u2["card_path"]).startswith("cards/"), str(dict(u2)))
    check("其余 5 张立绘行仍在",
          len(st.char_portrait_rows(CID)) == 5)

    # 写进回收站：_meta.json 必须能用于完整还原
    moved, trash_dir = api._trash_files(
        plugin, res["removed_files"], meta=res.get("removed_rows"))
    meta_path = Path(trash_dir) / "_meta.json"
    saved = json.loads(meta_path.read_text(encoding="utf-8"))["files"] if meta_path.is_file() else {}
    check("重试当日生成：回收站写下完整 _meta.json",
          moved == 1 and saved.get(files[2], {}).get("prompt") == "prompt-3"
          and "active" in saved.get(files[2], {}))

    # 立绘转参考图（旧版硬删的那条）：必须进回收站且行按主键删除
    target = files[4]
    check("待转移的是已停用立绘（验证停用态能随 meta 复原）",
          st.portrait_by_file(target)["active"] == 0)
    api._unlink_portrait(plugin, target)
    check("转移后原路径不再有立绘文件", not (td / target).is_file())
    check("转移后 portraits 行已删", st.portrait_by_file(target) is None)
    trash_files = list((td / "_trash").rglob(Path(target).name))
    check("转移的立绘进了回收站（不再硬删）", len(trash_files) == 1)
    metas = list((td / "_trash").rglob("_meta.json"))
    all_saved = {}
    for mf in metas:
        try:
            all_saved.update(json.loads(mf.read_text(encoding="utf-8")).get("files") or {})
        except Exception:
            pass
    check("转移的立绘 meta 保留停用态与提示词",
          int(all_saved.get(target, {}).get("active", 1)) == 0
          and all_saved.get(target, {}).get("prompt") == "prompt-5")

    # 批量清空（唯一宽删入口）也必须逐张留 meta
    st2 = st
    meta2 = {r["file"]: dict(r) for r in st2.char_portrait_rows(CID)}
    left = st2.clear_char_portraits(CID)
    moved2, trash_dir2 = api._trash_files(plugin, left, meta=meta2)
    saved2 = json.loads((Path(trash_dir2) / "_meta.json").read_text(
        encoding="utf-8"))["files"] if (Path(trash_dir2) / "_meta.json").is_file() else {}
    check("批量清空：N 张文件逐张写 meta（可完整还原）",
          moved2 == len(left) and len(saved2) == len(left),
          f"moved={moved2} meta={len(saved2)} files={len(left)}")
    check("批量清空后 portraits 行确实清空",
          st2.char_portrait_rows(CID) == [])
    st.close()

if FAILURES:
    print(f"test_portrait_delete_safety: {len(FAILURES)} FAILED")
    sys.exit(1)
print("test_portrait_delete_safety: ALL OK")
