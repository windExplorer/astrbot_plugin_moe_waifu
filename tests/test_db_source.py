# -*- coding: utf-8 -*-
"""v0.69.0 守卫：角色库 DB 真源（写入不落 JSON / 旧 JSON 只读冻结 / 升级灌库一次 / 导出）。

不启 astrbot，用真实 Store + webui_api 两个总闸（_json_save / _load_pack_file）
跑端到端。可直接运行，退出码即结论。
"""
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import store  # noqa: E402
import webui_api as api  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if not cond:
        FAILURES.append(name)
        print(f"FAIL: {name} {detail}")
    else:
        print(f"ok: {name}")


def dump(path: Path, pack: str, entries: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pack": pack, "title": pack, "enabled": True,
                                "entries": entries}, ensure_ascii=False),
                    encoding="utf-8")


with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    pools = root / "pools"
    dbp = root / "waifu.db"
    old_entry = {"id": "a1", "name": "旧角色", "series": "某作", "category": "anime",
                 "tags_en": "1girl", "strategy": {}}

    # --- 1) 老用户升级：旧 JSON 灌库一次（only_if_empty） ---
    dump(pools / "anime.json", "anime", [old_entry])
    st = store.Store(dbp)
    mig = st.migrate_relational(pools, root, only_if_empty=True)
    check("首次灌库：角色进 DB", "a1" in [e["id"] for e in st.load_pack_data("anime")["entries"]])
    st.close()

    # 模拟升级后第二次启动：篡改冻结 JSON，DB 非空应跳过灌库、不被覆盖
    dump(pools / "anime.json", "anime", [
        {"id": "evil", "name": "冻结文件被改", "series": "某作", "category": "anime",
         "tags_en": "1girl", "strategy": {}}])
    st2 = store.Store(dbp)
    mig2 = st2.migrate_relational(pools, root, only_if_empty=True)
    check("二次启动跳过灌库（DB 非空）", mig2.get("skipped_packs") == "db_nonempty", str(mig2))
    check("冻结 JSON 变更没有污染 DB 真源",
          "a1" in [e["id"] for e in st2.load_pack_data("anime")["entries"]]
          and "evil" not in [e["id"] for e in st2.load_pack_data("anime")["entries"]])

    # --- 2) 挂上 DB 写入钩子，写卷应直接落 DB、不碰 JSON 文件 ---
    api.set_pack_save(st2.save_pack_data)
    api.set_pack_sync(None)
    before_file = (pools / "anime.json").read_text(encoding="utf-8")
    plugin = SimpleNamespace(_data_dir=root, _store=st2)
    path, data = api._load_pack_file(plugin, "anime")
    check("_load_pack_file 从 DB 读到真源", any(e["id"] == "a1" for e in data["entries"]))
    check("_load_pack_file 返回的仍是虚拟 pools 路径", path.parent.name == "pools")
    data["entries"].append({"id": "a2", "name": "新角色", "series": "某作",
                            "category": "anime", "tags_en": "1girl", "strategy": {}})
    api._json_save(path, data)
    check("写入落 DB", "a2" in [e["id"] for e in st2.load_pack_data("anime")["entries"]])
    check("写入不碰 JSON 文件（底稿冻结）",
          (pools / "anime.json").read_text(encoding="utf-8") == before_file)

    # --- 3) 重开 DB：新编辑仍在（不回滚） ---
    st2.close()
    st3 = store.Store(dbp)
    check("重开后新编辑不回滚（DB 真源）",
          "a2" in [e["id"] for e in st3.load_pack_data("anime")["entries"]])

    # --- 4) 导出：DB→JSON 结构可用 ---
    exported = st3.export_packs()
    anime = next((p for p in exported if p["pack"] == "anime"), None)
    check("导出含 anime 卷且条目齐全",
          anime is not None and {e["id"] for e in anime["entries"]} >= {"a1", "a2"},
          str(anime and [e["id"] for e in anime["entries"]]))

    # --- 5) 类型改名同步角色 category ---
    st3.save_pack_data({"pack": "custom", "title": "自定义", "enabled": True,
                        "entries": [{"id": "c1", "name": "书角色", "series": "某书",
                                     "category": "custom", "tags_en": "1girl",
                                     "strategy": {}}]})
    st3.rename_pack("custom", "novel", "小说")
    c1 = next(e for e in st3.load_pack_data("novel")["entries"] if e["id"] == "c1")
    check("改名后角色 category 同步为新键", c1.get("category") == "novel", str(c1))
    st3.close()

# 还原全局钩子，避免污染同进程其它测试
api.set_pack_save(None)
api.set_pack_sync(None)

if FAILURES:
    print(f"test_db_source: {len(FAILURES)} FAILED")
    sys.exit(1)
print("test_db_source: ALL OK")
