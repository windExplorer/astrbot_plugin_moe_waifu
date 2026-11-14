# -*- coding: utf-8 -*-
"""characters.extra_json 扩展字段兜底列守卫（可直接运行，退出码即结论）。

复现用户报的固装保存丢失：编辑保存（keep_* 写进卷 JSON）→ sync_pack_file
同步入库 → 读回（load_packs 走新表）时非固定列字段被整批丢掉。修复后要求：
同步写 extra_json、读回 overlay，keep_* / profile / avatar / added_at 全保。
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pool  # noqa: E402
import store  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if not cond:
        FAILURES.append(name)
        print(f"FAIL: {name} {detail}")
    else:
        print(f"ok: {name}")


def main():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        pools = root / "pools"
        pools.mkdir(parents=True)
        entry = {
            "id": "witch", "name": "魔女", "series": "演示作品",
            "category": "anime", "tier": "S",
            "tags_en": "witch hat, cape",
            "strategy": {"type": "base"},
            "verified": True, "color": "#E8B4C8",
            # 非固定列的扩展字段
            "keep_headwear": True, "keep_outerwear": False,
            "keep_footwear": False,
            "profile": {"bio": "尖顶帽是本体", "birthday": "10月31日"},
            "avatar": "avatars/witch.png",
            "added_at": "2026-11-14",
        }
        pack = {"pack": "anime", "title": "演示卷", "enabled": True,
                "entries": [entry]}
        (pools / "anime.json").write_text(
            json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8")

        st = store.Store(root / "waifu.db")
        # 启动迁移：卷 JSON → 新表（main.initialize 同款调用）
        st.migrate_relational(pools, root)
        # 读路径（工坊/抽卡同款）
        packs = pool.load_packs(pools)
        check("读回走新表且有角色", bool(packs) and packs[0]["entries"])
        e = packs[0]["entries"][0]
        check("人设固装 keep_headwear 存活", e.get("keep_headwear") is True)
        check("keep_outerwear/keep_footwear 存活（False 也保留）",
              e.get("keep_outerwear") is False and e.get("keep_footwear") is False)
        check("profile 存活", e.get("profile", {}).get("bio") == "尖顶帽是本体")
        check("avatar / added_at 存活",
              e.get("avatar") == "avatars/witch.png"
              and e.get("added_at") == "2026-11-14")

        # 工坊保存链路同款：改 keep_outerwear=True → 写 JSON → sync → 读回
        raw = json.loads((pools / "anime.json").read_text(encoding="utf-8"))
        raw["entries"][0]["keep_outerwear"] = True
        (pools / "anime.json").write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        st.sync_pack_file(pools / "anime.json")
        packs = pool.load_packs(pools)
        e = packs[0]["entries"][0]
        check("改开关后再读回：keep_outerwear=True 存活",
              e.get("keep_outerwear") is True and e.get("keep_headwear") is True)

        # normalize_entry 输出仍带扩展字段（编辑保存 merge 链路依赖）
        n = pool.normalize_entry(e, "anime")
        check("normalize 输出带 keep_*", n["keep_headwear"] is True
              and n["keep_outerwear"] is True)
        st._conn.close()   # Windows 下先关连接才能清临时目录

    if FAILURES:
        print(f"test_store_extra: {len(FAILURES)} FAILURES")
        sys.exit(1)
    print("test_store_extra: ALL OK")


if __name__ == "__main__":
    main()
