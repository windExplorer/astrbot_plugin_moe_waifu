# -*- coding: utf-8 -*-
"""回收站自检（可直接运行，退出码即结论；不联网、不依赖 astrbot）。

守卫点：彻底删除时把行数据写进 `_meta.json`；清单能分组读出；还原能搬回
文件并按元数据重建立绘行；没有元数据时按归档文件名解析；目标已存在 /
路径穿越一律跳过；清空必须带 confirm。
"""
import asyncio
import json
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


class P:
    def __init__(self, root, st):
        self._data_dir = root
        self._store = st
        self.context = object()


async def _call(handler, plugin, body):
    async def fake_body():
        return body
    real = webui_api._body
    webui_api._body = fake_body
    try:
        return await handler(plugin)
    finally:
        webui_api._body = real


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    st = store.Store(root / "t.db")
    plugin = P(root, st)

    # 造一张立绘 + 对应行（先停用，彻底删除只对停用行开放）
    rel1 = "portraits/c1/2026-11-13_003_45231.png"
    (root / "portraits" / "c1").mkdir(parents=True)
    (root / rel1).write_bytes(PNG)
    st.add_portrait("c1", rel1, 45231, "2026-11-13", 3, "base",
                    prompt="测试提示词", theme="campus")
    st.set_portrait_active(rel1, False)

    r = asyncio.run(_call(webui_api.h_portrait_delete_hard, plugin,
                          {"file": rel1, "confirm": True}))
    check("彻底删除成功", r["status"] == "ok", str(r)[:200])
    trash_dir = Path(r["data"]["trash"])
    check("文件已在回收站", not (root / rel1).is_file()
          and (trash_dir / rel1).is_file(), str(trash_dir))
    meta_file = trash_dir / "_meta.json"
    check("回收站写入 _meta.json", meta_file.is_file())
    meta = json.loads(meta_file.read_text(encoding="utf-8"))["files"]
    check("meta 记录了行数据",
          meta[rel1]["char_id"] == "c1" and int(meta[rel1]["seq"]) == 3
          and int(meta[rel1]["seed"]) == 45231
          and meta[rel1]["prompt"] == "测试提示词", str(meta.get(rel1)))

    # 清单
    d = asyncio.run(_call(webui_api.h_trash_list, plugin, {}))["data"]
    check("清单分组与计数", d["total_files"] == 1 and len(d["groups"]) == 1, str(d)[:200])
    f = d["groups"][0]["files"][0]
    check("清单项带 meta_ok / char_id / sizeText 输入",
          f["meta_ok"] is True and f["char_id"] == "c1" and f["rel"] == rel1, str(f))

    # 还原：搬回原位 + 重建行
    d2 = asyncio.run(_call(webui_api.h_trash_restore, plugin,
                           {"items": [{"stamp": trash_dir.name, "rel": rel1}]}))["data"]
    check("还原搬回文件并重建行",
          d2["restored"] == 1 and d2["rebuilt"] == 1, str(d2))
    check("文件回到原位", (root / rel1).is_file())
    row = st.portrait_by_file(rel1)
    check("立绘行按元数据重建",
          row is not None and int(row["seq"]) == 3 and int(row["seed"]) == 45231
          and str(row.get("theme") or "") == "campus", str(dict(row or {})))
    check("还原后仍是停用状态（不擅自占配额）", int(row["active"]) == 0)

    # 无 meta：按文件名解析还原
    rel2 = "portraits/c2/2026-11-12_007_12345.png"
    (root / "portraits" / "c2").mkdir(parents=True)
    manual = root / "_trash" / "20261113-999999" / rel2
    manual.parent.mkdir(parents=True)
    manual.write_bytes(PNG)
    d3 = asyncio.run(_call(webui_api.h_trash_restore, plugin,
                           {"items": [{"stamp": "20261113-999999", "rel": rel2}]}))["data"]
    check("无元数据也能按文件名还原", d3["restored"] == 1 and d3["rebuilt"] == 1, str(d3))
    row2 = st.portrait_by_file(rel2)
    check("文件名解析出 char_id / seq / seed",
          row2 is not None and int(row2["seq"]) == 7 and int(row2["seed"]) == 12345,
          str(dict(row2 or {})))

    # 目标已存在 / 路径穿越
    again = root / "_trash" / "20261113-999999" / rel2
    again.parent.mkdir(parents=True, exist_ok=True)
    again.write_bytes(PNG)
    d4 = asyncio.run(_call(webui_api.h_trash_restore, plugin,
                           {"items": [{"stamp": "20261113-999999", "rel": rel2},
                                      {"stamp": "20261113-999999", "rel": "../escape.png"}]}))["data"]
    check("目标已存在与路径穿越都跳过",
          d4["restored"] == 0 and d4["skipped_count"] == 2, str(d4))

    # 清空
    check("清空必须 confirm",
          asyncio.run(_call(webui_api.h_trash_purge, plugin, {}))["status"] == "error")
    d5 = asyncio.run(_call(webui_api.h_trash_purge, plugin,
                           {"confirm": True, "stamps": ["20261113-999999"]}))["data"]
    check("按组清空", d5["dirs"] == 1, str(d5))
    check("该组目录已删除",
          not (root / "_trash" / "20261113-999999").exists())
    st.close()

print("test_trash: ALL OK")
