# -*- coding: utf-8 -*-
"""卡片调试（/debug/card）自检：模拟抽卡直接出卡，但不写库 / 不生图。

可直接运行（退出码即结论）：临时 data 目录 + 真 SQLite + SimpleNamespace 桩插件，
直接喂 `webui_api._card_debug_core`。要 Pillow：

    uv run --no-project --python 3.12 --with pillow --with aiohttp python tests/test_card_debug.py

守卫的是这个功能最容易翻车的五点：
1) 随机抽走真实种子链路，且「跳过没立绘的角色」不会卡死；
2) 不出卡时给得出原因（不是静默失败）；
3) 覆盖档位 / 简介 / 印象语只影响这一次渲染，不写回角色库；
4) 全程不写 draws、不新增 portraits 行（用户最怕调试污染正式记录）；
5) debug_cards/ 只留最近 N 张（调试产物不该无限长）。
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
from PIL import Image  # noqa: E402

TODAY = "2026-11-12"
FAILURES = []


def check(name, cond, detail=""):
    if not cond:
        FAILURES.append(f"{name} {detail}")
        print(f"FAIL: {name} {detail}")
    else:
        print(f"ok: {name}")


def make_plugin(root: Path, st) -> SimpleNamespace:
    pools = root / "pools"
    pools.mkdir(parents=True, exist_ok=True)
    (pools / "anime.json").write_text(json.dumps({
        "pack": "anime", "title": "动漫卷", "enabled": True,
        "entries": [
            # a_char 故意不给立绘：用来验证「跳过没立绘的角色」
            {"id": "a_char", "name": "没图角色", "series": "测试作品",
             "category": "anime", "tier": "B", "tags_en": "a, blue hair",
             "color": "#60A5FA", "verified": True,
             "profile": {"tagline": "库里的一句话"}},
            {"id": "b_char", "name": "有图角色", "series": "测试作品",
             "category": "anime", "tier": "A", "tags_en": "b, red hair",
             "color": "#D63384", "verified": True,
             "profile": {"tagline": "库里的一句话"}},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    cfg = {
        "timezone": "Asia/Shanghai", "salt": "test-salt",
        "group_pools": "", "allow_unverified": False,
        "portraits_per_char": 5,
    }
    return SimpleNamespace(
        _store=st, _data_dir=root,
        config=cfg,
        _cfg=lambda key, default=None: cfg.get(key, default),
        _today=lambda: TODAY,
    )


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    st = store.Store(tmp / "waifu.db")
    plugin = make_plugin(tmp, st)

    portraits = tmp / "portraits"
    portraits.mkdir(parents=True, exist_ok=True)
    for seq in (1, 2):
        p = portraits / f"b{seq}.png"
        Image.new("RGB", (700, 1200), (120 + seq * 10, 180, 200)).save(p)
    st.add_portrait("b_char", "portraits/b1.png", 11, TODAY, 1, "base")
    st.add_portrait("b_char", "portraits/b2.png", 22, TODAY, 2, "base")
    rows_before = len(st.list_portraits("b_char"))

    # ---------- 1) 随机抽：真实种子链路 + 跳过没立绘的角色 ----------
    ok_hits, miss_hits = 0, 0
    for i in range(8):
        uid = f"2000{i}"
        res = api._card_debug_core(plugin, {"mode": "random", "user_id": uid,
                                            "date": TODAY})
        if res.get("ok"):
            ok_hits += 1
            check(f"随机抽 {uid} 只落在有立绘的角色", res["char"]["char_id"] == "b_char",
                  res["char"]["char_id"])
            card_file = tmp / res["card_path"]
            check(f"随机抽 {uid} 卡图落盘", card_file.is_file())
            check(f"随机抽 {uid} 预览是 data URL",
                  str(res.get("preview") or "").startswith("data:image/jpeg;base64,"))
            check(f"随机抽 {uid} 在 debug_cards/ 下",
                  res["card_path"].startswith(api._CARD_DEBUG_DIR + "/"))
        else:
            miss_hits += 1
    check("随机抽能出卡", ok_hits > 0, f"ok={ok_hits}")
    check("不写抽签记录（dry-run）", st.get_draw("20000", TODAY) is None)
    check("不新增立绘行", len(st.list_portraits("b_char")) == rows_before)

    # 关掉「跳过没立绘」时，抽到 a_char 必须明确说原因而不是静默失败
    reasons = []
    for i in range(40):
        res = api._card_debug_core(plugin, {"mode": "random", "user_id": f"9{i:03d}",
                                            "date": TODAY, "ensure_portrait": False})
        if not res.get("ok"):
            reasons.append(str(res.get("reason") or ""))
    check("无立绘角色给出人话原因",
          bool(reasons) and any("立绘" in r for r in reasons), str(reasons[:1]))

    # ---------- 2) 指定角色 + 覆盖项只影响本次渲染 ----------
    res = api._card_debug_core(plugin, {
        "mode": "char", "char_id": "b_char", "date": TODAY,
        "tier": "S", "quote": "调试用台词", "tagline": "调试用简介",
        "same_count": 9, "total_count": 3, "rerolled": True,
        "portrait": "latest",
    })
    check("指定角色出卡成功", bool(res.get("ok")), str(res.get("reason")))
    check("档位覆盖生效", res["char"]["tier"] == "S", res["char"]["tier"])
    check("印象语覆盖生效", res["quote"] == "调试用台词", res["quote"])
    check("取图序号对得上 latest", res["portrait"]["index"] == 1,
          str(res["portrait"]))
    import pool as pool_mod
    packs = pool_mod.load_packs(tmp / "pools")
    entry_now = pool_mod.find_entry(packs, "b_char")
    check("覆盖不写回角色库（档位仍是 A）", entry_now["tier"] == "A", entry_now["tier"])
    check("覆盖不写回角色库（简介没被改）",
          str((entry_now.get("profile") or {}).get("tagline")) == "库里的一句话")

    # ---------- 3) 失败路径 ----------
    res = api._card_debug_core(plugin, {"mode": "char", "char_id": "a_char",
                                        "date": TODAY})
    check("没立绘的角色明确拒绝", not res.get("ok") and "立绘" in str(res.get("reason")),
          str(res.get("reason")))
    res = api._card_debug_core(plugin, {"mode": "char", "char_id": "不存在",
                                        "date": TODAY})
    check("不存在的角色明确拒绝",
          not res.get("ok") and "没找到" in str(res.get("reason")), str(res.get("reason")))

    # ---------- 4) debug_cards/ 只留最近 N 张 ----------
    out_dir = tmp / api._CARD_DEBUG_DIR
    for i in range(api._CARD_DEBUG_KEEP + 6):
        (out_dir / f"old_{i}.png").write_bytes(b"x")
    api._card_debug_core(plugin, {"mode": "char", "char_id": "b_char", "date": TODAY})
    keep = len(list(out_dir.glob("*.png")))
    check("调试卡按上限清理", keep <= api._CARD_DEBUG_KEEP, f"实际 {keep}")

    st.close()

if FAILURES:
    print(f"test_card_debug: {len(FAILURES)} FAILED")
    sys.exit(1)
print("test_card_debug: ALL OK")
