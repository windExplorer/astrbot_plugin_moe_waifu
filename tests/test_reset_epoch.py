# -*- coding: utf-8 -*-
"""v0.9.5 三件套自检：种子 epoch 重派 / 清签扣减计数 / 印象语多条与按签选句。

直接运行，退出码即结论；临时库落 tempfile，不污染仓库。
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pool  # noqa: E402
import store as store_mod  # noqa: E402


def check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    print(f"ok: {name}")


# ---------- 1. 种子 epoch ----------
s0 = pool.derive_seed("2026-11-12", "u1", "salt", 0)
s0b = pool.derive_seed("2026-11-12", "u1", "salt", 0, 0)
check("epoch=0 与旧调用等价", s0 == s0b)
s1 = pool.derive_seed("2026-11-12", "u1", "salt", 0, 1)
check("epoch+1 种子变", s0 != s1)
check("epoch 同值恒定", s1 == pool.derive_seed("2026-11-12", "u1", "salt", 0, 1))

# ---------- 2. 印象语：归一化 + 按签选句 ----------
e = pool.normalize_entry({
    "id": "a", "name": "A", "tags_en": "1girl",
    "quote": "旧的单句",
    "quotes": ["第二句", "第三句"],
}, "p")
check("quotes 合并旧 quote 在首位",
      e["quotes"] == ["旧的单句", "第二句", "第三句"] and e["quote"] == "旧的单句")
q1 = pool.pick_quote(e, s0)
check("pick_quote 恒定", q1 == pool.pick_quote(e, s0) and q1 in e["quotes"])
q2 = pool.pick_quote(e, s1)
check("种子变句子可变", q1 in e["quotes"] and q2 in e["quotes"])
e_legacy = pool.normalize_entry({"id": "b", "name": "B", "tags_en": "1girl",
                                 "quote": "只有旧字段"}, "p")
check("旧单条字段兼容", e_legacy["quotes"] == ["只有旧字段"])
check("无印象语回落空串", pool.pick_quote({"id": "c", "name": "C", "tags_en": "1girl"}, s0) == "")

# ---------- 3. store：清签扣减计数 + epoch 重派 ----------
with tempfile.TemporaryDirectory() as td:
    st = store_mod.Store(Path(td) / "waifu.db")

    def seed_once(uid, cid, nonce=0):
        st.upsert_draw(uid, "2026-11-12", cid, nonce, False, "nick", "g1", "群", "aiocqhttp",
                       seed_hex=pool.derive_seed("2026-11-12", uid, "s", nonce).hex(),
                       char_name=cid.upper(), series="", portrait_file="", decision="generate")
        st.bump_user_char(uid, cid)
        st.bump_char_stats(cid, "2026-11-12")

    seed_once("u1", "charA")
    seed_once("u1", "charA", nonce=1)   # 换老婆后又是 charA：counts 应记 2 次
    seed_once("u2", "charA")
    check("user_char_counts 记 2 次",
          st._conn.execute("SELECT count FROM user_char_counts WHERE user_id='u1' "
                           "AND char_id='charA'").fetchone()["count"] == 2)
    check("char_stats 记 3 次",
          st._conn.execute("SELECT total FROM char_stats WHERE char_id='charA'"
                           ).fetchone()["total"] == 3)

    rows = st.clear_draws("u1", "2026-11-12")
    check("清签返回 1 行", len(rows) == 1)
    check("u1 计数扣回 0 且行删除",
          st._conn.execute("SELECT COUNT(*) AS n FROM user_char_counts "
                           "WHERE user_id='u1'").fetchone()["n"] == 0)
    check("char_stats 扣到 1（u2 还在）",
          st._conn.execute("SELECT total FROM char_stats WHERE char_id='charA'"
                           ).fetchone()["total"] == 1)
    check("u1 epoch=1", st.get_epoch("u1") == 1)
    check("u2 epoch=0", st.get_epoch("u2") == 0)
    st.clear_draws("u1", "2026-11-12")  # 再清一次（无数据也应安全）
    check("重复清签 epoch 不再涨（无受影响行）", st.get_epoch("u1") == 1)
    st.clear_draws("u2", "2026-11-12")
    check("char_stats 清零后行删除",
          st._conn.execute("SELECT COUNT(*) AS n FROM char_stats").fetchone()["n"] == 0)
    check("u2 epoch=1", st.get_epoch("u2") == 1)
    st.close()

print("test_reset_epoch: ALL OK")
