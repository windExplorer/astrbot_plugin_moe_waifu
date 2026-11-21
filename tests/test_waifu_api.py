# -*- coding: utf-8 -*-
"""waifu.v1 跨插件契约守卫：main_ref_map 只暴露角色名/tags_en/主参考图（strategy.ref）。

给萌绘（astrbot-comfyui-anima）读取用。要点：
- 只取 strategy.ref（主参考），不碰 refs 资料库 / 立绘 / 三视图；
- ref 解析成绝对路径 / URL，失效的进 missing（WebUI 提示，不参与生图）；
- 契约方法都是 async（内部 to_thread），返回 status 信封，异常安全降级。
"""
import ast
import asyncio
import json
import re
import sys
import tempfile
import textwrap
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import store  # noqa: E402
import draw  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if not cond:
        FAILURES.append(name)
        print(f"FAIL: {name} {detail}")
    else:
        print(f"ok: {name}")


# --- 抠出 main.py 里的 WaifuApi 类，配假 self 执行（main 依赖 astrbot 运行时装不了）---
_src = (ROOT / "main.py").read_text(encoding="utf-8")
_tree = ast.parse(_src)
_cls = next(n for n in _tree.body if isinstance(n, ast.ClassDef) and n.name == "WaifuApi")
_ns = {"asyncio": asyncio, "re": re, "draw": draw}
exec(textwrap.dedent(ast.get_source_segment(_src, _cls)), _ns)
WaifuApi = _ns["WaifuApi"]

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    (root / "refs").mkdir()
    good = root / "refs" / "a.png"
    good.write_bytes(b"fake")

    st = store.Store(str(root / "waifu.db"))
    entries = [
        {"id": "char_a", "name": "角色甲", "category": "anime", "tags_en": "1girl, blue eyes",
         "aliases": ["甲酱"], "series": "作品A", "strategy": {"ref": "refs/a.png", "refs": ["refs/b.png"]}},
        {"id": "char_b", "name": "角色乙", "category": "anime", "tags_en": "1boy",
         "strategy": {"ref": ""}},  # 主参考为空 → missing
    ]
    st.save_pack_data({"pack": "anime", "title": "anime", "entries": entries})

    fake_plugin = SimpleNamespace(_store=st, _data_dir=root)
    api = WaifuApi(fake_plugin)

    check("fingerprint = waifu.v1:1", api.fingerprint() == "waifu.v1:1", api.fingerprint())

    res = asyncio.run(api.main_ref_map())
    check("status ok", res.get("status") == "ok", str(res)[:200])
    data = res["data"]
    check("两个角色都返回", set(data) == {"char_a", "char_b"}, str(list(data)))
    check("主参考解析成绝对路径", data["char_a"]["ref"] == str(good.resolve()), data["char_a"]["ref"])
    check("只取 ref 不取 refs 资料库", "refs/b.png" not in json.dumps(data, ensure_ascii=False))
    check("tags_en 透传", data["char_a"]["tags_en"] == "1girl, blue eyes")
    check("别名透传", data["char_a"]["aliases"] == ["甲酱"])
    check("空主参考进 missing", res["missing"] == ["char_b"], str(res["missing"]))

    # 未初始化时安全降级
    res2 = asyncio.run(WaifuApi(SimpleNamespace(_store=None, _data_dir=None)).main_ref_map())
    check("未初始化 → error 信封", res2.get("status") == "error", str(res2)[:120])

    st.close()

if FAILURES:
    print("FAILURES:", FAILURES)
    sys.exit(1)
print("waifu.v1 api OK")
