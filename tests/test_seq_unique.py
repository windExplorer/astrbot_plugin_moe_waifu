# -*- coding: utf-8 -*-
"""立绘 seq 唯一序列自检（可直接运行，退出码即结论；不联网、不依赖 astrbot）。

守卫点：硬删最大号后 seq 不回退、原子分配唯一且并发不撞号、重启持久化、
老库按 MAX(seq) 播种、展示编号与唯一序列分离。
"""
import asyncio
import sqlite3
import sys
import tempfile
import threading
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


with tempfile.TemporaryDirectory() as td:
    db = Path(td) / "waifu.db"
    st = store.Store(db)

    # 1) 硬删最大 seq：旧口径 MAX(seq)+1 会回退并复用号，计数器不会
    for s in range(4):
        st.add_portrait("c", f"portraits/c/{s}.png", 100 + s,
                        "2026-11-13", s, "base")
    check("初始下一个 seq = 4", st.next_portrait_seq("c") == 4)
    st.delete_portrait("portraits/c/3.png")
    check("硬删最大号后 seq 不回退", st.next_portrait_seq("c") == 4)
    st.delete_portrait("portraits/c/2.png")
    check("连删两张仍不回退", st.next_portrait_seq("c") == 4)

    # 2) 原子分配：跳过已用号，预览只读不消耗
    got = [st.alloc_portrait_seq("c") for _ in range(3)]
    check("alloc 连续唯一且从 4 起", got == [4, 5, 6], str(got))
    st.add_portrait("c", "portraits/c/4.png", 200, "2026-11-13", got[0], "base")
    check("写入不倒退计数器", st.next_portrait_seq("c") == 7)
    check("预览只读不消耗号",
          st.next_portrait_seq("c") == 7
          and st.next_portrait_seq("c") == 7)

    # 3) 并发分配不得撞号（抽卡 / 工坊补图同时跑）
    results = []
    rlock = threading.Lock()

    def worker():
        local = [st.alloc_portrait_seq("cc") for _ in range(20)]
        with rlock:
            results.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("并发 120 次分配全唯一",
          len(results) == 120 and len(set(results)) == 120,
          f"total={len(results)} unique={len(set(results))}")

    # 4) 重启后计数器仍在（持久化，不依赖内存状态）
    st.close()
    st2 = store.Store(db)
    check("重启后 seq 不回退", st2.next_portrait_seq("c") == 7)
    check("重启后 alloc 接着走", st2.alloc_portrait_seq("c") == 7)
    st2.close()

    # 5) 老库升级：只有 portraits、没有 portrait_seq 行时按 MAX(seq) 播种
    old_db = Path(td) / "old.db"
    con = sqlite3.connect(old_db)
    con.executescript(
        "CREATE TABLE portraits (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "char_id TEXT, file TEXT, seed INTEGER, date TEXT, seq INTEGER, "
        "strategy TEXT, created_at TEXT);")
    con.execute(
        "INSERT INTO portraits(char_id,file,seed,date,seq,strategy,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        ("x", "portraits/x/9.png", 1, "2026-11-13", 9, "base",
         "2026-11-13T00:00:00Z"))
    con.commit()
    con.close()
    st3 = store.Store(old_db)
    check("老库按 MAX(seq) 播种计数器", st3.next_portrait_seq("x") == 10,
          str(st3.next_portrait_seq("x")))
    st3.delete_portrait("portraits/x/9.png")
    check("老库删行后也不回退", st3.next_portrait_seq("x") == 10)
    st3.close()

    # 6) 展示编号连续，唯一序列保持原值（可跳号）
    with tempfile.TemporaryDirectory() as td2:
        root = Path(td2)
        st4 = store.Store(root / "t.db")
        for s in (0, 1, 5, 9):
            st4.add_portrait("z", f"portraits/z/{s}.png", 1,
                             "2026-11-13", s, "base")
        st4.set_portrait_active("portraits/z/1.png", False)

        class P:
            _data_dir = root
            _store = st4

        async def _call():
            async def fake_body():
                return {"char_id": "z"}
            real = webui_api._body
            webui_api._body = fake_body
            try:
                return await webui_api.h_portraits_list(P())
            finally:
                webui_api._body = real

        items = asyncio.run(_call())["data"]["items"]
        check("display_no 连续 1..N（含停用行）",
              [i["display_no"] for i in items] == [1, 2, 3, 4],
              str([i["display_no"] for i in items]))
        check("seq 保持唯一原值、允许跳号",
              [i["seq"] for i in items] == [0, 1, 5, 9],
              str([i["seq"] for i in items]))
        st4.close()

print("test_seq_unique: ALL OK")
