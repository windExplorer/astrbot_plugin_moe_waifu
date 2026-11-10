# -*- coding: utf-8 -*-
"""pool.py 纯函数自检（可直接运行，退出码即结论）。"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pool  # noqa: E402


def check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    print(f"ok: {name}")


# 种子决定论
s1 = pool.derive_seed("2026-11-11", "u1", "salt", 0)
s2 = pool.derive_seed("2026-11-11", "u1", "salt", 0)
s3 = pool.derive_seed("2026-11-11", "u1", "salt", 1)
check("derive_seed 恒定", s1 == s2)
check("nonce 变种子变", s1 != s3)

# 抽取恒定
cands = [
    {"id": "a", "name": "甲", "pack": "p1", "tags_en": "x"},
    {"id": "b", "name": "乙", "pack": "p1", "tags_en": "y"},
    {"id": "c", "name": "丙", "pack": "p2", "tags_en": "z"},
]
e1 = pool.pick_character(s1, cands)
e2 = pool.pick_character(pool.derive_seed("2026-11-11", "u1", "salt", 0), cands)
check("pick_character 恒定", e1["id"] == e2["id"] and e1["id"] in {"a", "b", "c"})
try:
    pool.pick_character(s1, [])
    check("空候选应抛 ValueError", False)
except ValueError:
    check("空候选抛 ValueError", True)

# 立绘种子 / 满库取图
p1 = pool.portrait_seed_int("miku", "2026-11-11", 0, "salt")
p2 = pool.portrait_seed_int("miku", "2026-11-11", 0, "salt")
p3 = pool.portrait_seed_int("miku", "2026-11-11", 1, "salt")
check("portrait_seed_int 恒定", p1 == p2 and p1 != p3 and 0 <= p1 < 2 ** 31)
idx1 = pool.pool_index("miku", "2026-11-11", 5, "salt")
idx2 = pool.pool_index("miku", "2026-11-11", 5, "salt")
check("pool_index 恒定且在范围内", idx1 == idx2 and 0 <= idx1 < 5)
check("pool_index 空库安全", pool.pool_index("miku", "d", 0, "salt") == 0)

# 群绑定解析
bound = pool.parse_group_pools("123=a,b\n\n坏行\n456= c ， d \n456=a")
check("parse_group_pools 同群多行合并去重", bound == {"123": ["a", "b"], "456": ["c", "d", "a"]})
check("未绑定群返回 None", pool.pack_names_for_group(bound, "999") is None)
check("绑定群返回列表", pool.pack_names_for_group(bound, "123") == ["a", "b"])
check("私聊(空gid)返回 None", pool.pack_names_for_group(bound, "") is None)

# 条目归一化
good = {"id": "x", "name": "名", "tags_en": "t", "strategy": {"type": "LORA"}, "category": "xxx"}
n = pool.normalize_entry(good, "p")
check("normalize_entry 保留合法条目", n is not None)
check("strategy 大写转小写", n["strategy_type"] == "lora")
check("未知分类落 other", n["category"] == "other")
check("缺 tags 拒收", pool.normalize_entry({"id": "x", "name": "名"}, "p") is None)
check("缺 id 拒收", pool.normalize_entry({"name": "名", "tags_en": "t"}, "p") is None)

# 候选过滤：verified / 群绑定 / 去重 / 排序
packs = [
    {"pack": "p1", "title": "卷一", "enabled": True, "entries": [
        {"id": "b", "name": "乙", "tags_en": "y", "verified": True},
        {"id": "a", "name": "甲", "tags_en": "x", "verified": False},
    ]},
    {"pack": "p2", "title": "卷二", "enabled": True, "entries": [
        {"id": "c", "name": "丙", "tags_en": "z", "verified": True},
        {"id": "b", "name": "乙2", "tags_en": "y2", "verified": True},  # 跨卷重复 id
    ]},
    {"pack": "p3", "title": "停用卷", "enabled": False, "entries": [
        {"id": "d", "name": "丁", "tags_en": "w", "verified": True},
    ]},
]
all_c = pool.candidates_for(packs, "", "", False)
check("verified 过滤+去重+排序", [e["id"] for e in all_c] == ["b", "c"])
check("allow_unverified 放宽", len(pool.candidates_for(packs, "", "", True)) == 3)
bound_c = pool.candidates_for(packs, "123", "123=p2", False)
check("群绑定过滤（p2 含重复 id 也照常出）", [e["id"] for e in bound_c] == ["b", "c"])
check("未绑定群=全卷", pool.candidates_for(packs, "999", "123=p2", False) == all_c)

# 查找
found = pool.find_entry(packs, "c")
check("find_entry 按 id", found is not None and found["name"] == "丙")
check("find_entry 按名", pool.find_entry(packs, "乙") is not None)
check("find_entry id 大小写不敏感", pool.find_entry(packs, "B") is not None)
check("find_entry 未命中", pool.find_entry(packs, "不存在") is None)

# 种子卷安装：只装缺失卷，绝不覆盖
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    bundled = td / "seed_pool.json"
    bundled.write_text(json.dumps({"packs": [
        {"pack": "p1", "title": "卷一", "entries": [{"id": "a", "name": "甲", "tags_en": "x"}]},
        {"pack": "p2", "title": "卷二", "entries": [{"id": "b", "name": "乙", "tags_en": "y"}]},
    ]}, ensure_ascii=False), encoding="utf-8")
    pools = td / "pools"
    pools.mkdir()
    (pools / "p1.json").write_text('{"pack":"p1","title":"用户改过的","entries":[]}', encoding="utf-8")
    installed = pool.install_seed_pool(pools, bundled)
    check("只装缺失卷", installed == ["p2"])
    kept = json.loads((pools / "p1.json").read_text(encoding="utf-8"))
    check("不覆盖已有卷", kept["title"] == "用户改过的")
    packs2 = pool.load_packs(pools)
    check("load_packs 读回", {p["pack"] for p in packs2} == {"p1", "p2"})

print("test_pool: ALL OK")
