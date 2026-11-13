# -*- coding: utf-8 -*-
"""批量停用 / 启用立绘 handler 自检（可直接运行，退出码即结论）。

守卫点：批量停用与恢复真正改变配额计数、非法路径与不存在的行只跳过不整批
失败、空 / 非数组参数被拒。
"""
import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import store  # noqa: E402
import webui_api  # noqa: E402


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL: {name} {detail}")
        sys.exit(1)
    print(f"ok: {name}")


async def _call(handler, plugin, body):
    async def fake_body():
        return body
    real = webui_api._body
    webui_api._body = fake_body
    try:
        return await handler(plugin)
    finally:
        webui_api._body = real


with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    st = store.Store(root / "t.db")
    for i in range(4):
        st.add_portrait("c", f"portraits/c/{i}.png", 1, "2026-11-13", i, "base")

    class P:
        _data_dir = root
        _store = st

    d = asyncio.run(_call(webui_api.h_portraits_set_active, P(),
                          {"files": ["portraits/c/0.png", "portraits/c/1.png"],
                           "active": False}))["data"]
    check("批量停用 2 张", d["changed"] == 2 and d["active"] is False, str(d))
    check("停用后不占配额", st.count_portraits("c") == 2)

    d2 = asyncio.run(_call(webui_api.h_portraits_set_active, P(),
                           {"files": ["portraits/c/0.png", "portraits/c/1.png"],
                            "active": True}))["data"]
    check("批量启用回来", d2["changed"] == 2 and st.count_portraits("c") == 4,
          str(d2))

    # 脏数据不能拖垮整批：非法路径 / 不存在的行 / 空串各记一次 skipped
    d3 = asyncio.run(_call(webui_api.h_portraits_set_active, P(),
                           {"files": ["portraits/c/2.png", "../escape.png",
                                      "portraits/c/none.png", ""],
                            "active": False}))["data"]
    check("脏路径只跳过不整批失败",
          d3["changed"] == 1 and d3["skipped_count"] == 3, str(d3))
    check("合法那条已生效", st.count_portraits("c") == 3)

    check("空 files 被拒",
          asyncio.run(_call(webui_api.h_portraits_set_active, P(),
                            {"files": []}))["status"] == "error")
    check("非数组 files 被拒",
          asyncio.run(_call(webui_api.h_portraits_set_active, P(),
                            {"files": "x"}))["status"] == "error")
    st.close()

print("test_portrait_bulk: ALL OK")
