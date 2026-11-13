# -*- coding: utf-8 -*-
"""批量补图范围筛选自检（可直接运行，退出码即结论；不联网、不依赖 astrbot）。

守卫点：`mode=series / packs / chars` 在**空选择**时不得过滤掉全部角色
（旧写法 `not sel_x or ...` 会 continue 掉所有人 → 「不选作品就补不了图」）；
只有真的勾了选项才收窄；未验证角色始终排除。
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import webui_api  # noqa: E402


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL: {name} {detail}")
        sys.exit(1)
    print(f"ok: {name}")


def E(cid, name, series, verified=True):
    return {"id": cid, "name": name, "series": series,
            "tags_en": "1girl", "verified": verified,
            "strategy": {"type": "base"}}


with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    pools = root / "pools"
    pools.mkdir()
    (pools / "game.json").write_text(json.dumps({
        "pack": "game", "title": "游戏", "enabled": True,
        "entries": [
            E("ganyu", "甘雨", "原神"),
            E("verina", "维里奈", "鸣潮"),
            E("nanally", "娜娜莉", "异环"),
            E("march", "三月七", "崩坏：星穹铁道", verified=False),
        ],
    }, ensure_ascii=False), encoding="utf-8")
    (pools / "anime.json").write_text(json.dumps({
        "pack": "anime", "title": "动漫", "enabled": True,
        "entries": [E("miku", "初音未来", "VOCALOID")],
    }, ensure_ascii=False), encoding="utf-8")

    class P:
        _data_dir = root

    ALL = {"ganyu", "verina", "nanally", "miku"}

    def ids(body):
        return {e["id"] for e in webui_api._batch_scope_entries(P(), body)}

    # 核心：空选择 = 全部作品（不再 0 命中）
    check("按作品但没选 → 全部作品", ids({"mode": "series", "series": []}) == ALL,
          str(ids({"mode": "series", "series": []})))
    check("按卷但没选 → 全部卷", ids({"mode": "packs", "packs": []}) == ALL)
    check("按角色但没选 → 全部角色", ids({"mode": "chars", "chars": []}) == ALL)
    check("完全不传范围字段 → 全部", ids({}) == ALL)

    # 选了才收窄
    check("选作品收窄", ids({"mode": "series", "series": ["原神"]}) == {"ganyu"})
    check("选多个作品取并集",
          ids({"mode": "series", "series": ["原神", "鸣潮"]}) == {"ganyu", "verina"})
    _by_pack = ids({"mode": "packs", "packs": ["anime"]})
    check("选卷收窄", _by_pack == {"miku"}, str(_by_pack))
    _by_char = ids({"mode": "chars", "chars": ["ganyu"]})
    check("选角色收窄", _by_char == {"ganyu"}, str(_by_char))

    # 占位 / 脏值容错
    check("空串占位被忽略（仍是全部）",
          ids({"mode": "series", "series": [""]}) == ALL)
    check("空串 + 真实值的组合只按真实值收窄",
          ids({"mode": "series", "series": ["", "原神"]}) == {"ganyu"})

    # 其它前提不能被破坏
    check("未验证角色始终排除", "march" not in ids({"mode": "all"}))
    check("mode=all 忽略选择项", ids({"mode": "all", "series": ["原神"]}) == ALL)

print("test_batch_scope: ALL OK")
