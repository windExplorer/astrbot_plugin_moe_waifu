# -*- coding: utf-8 -*-
"""抽卡公平性守卫（可直接运行，退出码即结论）。

三件事：
1) **角色基础概率完全相等**（无历史时均匀，回归用 10 万次模拟盯住）；
2) **近期重复降权**只降不灭：昨天抽过的角色权重 ×0.25，长期「连续两天同角色」
   的比例明显低于基线，但每个角色永远有权重 > 0；
3) **立绘按用户遍历**：同一角色重复抽到时换一张没见过的，整轮看完才进下一轮，
   新一轮第一张不与上一轮最后一张相同；补进新图后能被优先轮到。

跑法：uv run --no-project --python 3.12 python tests/test_fairness.py
（`draw_history` 那组会建临时 SQLite，不需要 Pillow / astrbot。）
"""
import collections
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pool  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if not cond:
        FAILURES.append(f"{name} {detail}")
        print(f"FAIL: {name} {detail}")
    else:
        print(f"ok: {name}")


def cands(n):
    return [{"id": f"c{i}", "name": f"角色{i}"} for i in range(n)]


TODAY = "2026-11-15"

# ---------- 1) 基础概率完全相等 ----------
C = cands(40)
counter = collections.Counter()
N = 100000
for i in range(N):
    seed = pool.derive_seed(TODAY, f"u{i}", "salt", 0, 0)
    counter[pool.pick_character(seed, C)["id"]] += 1
exp = N / len(C)
dev = max(abs(v - exp) / exp for v in counter.values())
check("无历史时角色等概率（10 万次，偏差 < 15%）", len(counter) == len(C) and dev < 0.15,
      f"偏差 {dev * 100:.1f}%")

# 加权接口在「权重全等」时也必须等价于等概率（不要引入新偏差）
counter2 = collections.Counter()
w_equal = [1.0] * len(C)
for i in range(20000):
    seed = pool.derive_seed(TODAY, f"w{i}", "salt", 0, 0)
    counter2[pool.pick_character_weighted(seed, C, w_equal)["id"]] += 1
dev2 = max(abs(v - 20000 / len(C)) / (20000 / len(C)) for v in counter2.values())
check("权重全等时与等概率一致（2 万次，偏差 < 20%）", dev2 < 0.20,
      f"偏差 {dev2 * 100:.1f}%")

# ---------- 2) 近期重复降权 ----------
check("权重表：今天 / 昨天 / 2 天 / 4 天 / 7 天",
      [round(pool.recency_weight(d), 3) for d in (0, 1, 2, 4, 7)] ==
      [0.15, 0.25, 0.40, 0.55, 0.75])
check("没抽过 / 太久以前 = 1.0",
      pool.recency_weight(None) == 1.0 and pool.recency_weight(30) == 1.0)
check("强度 0 = 关闭（恒 1.0）", pool.recency_weight(0, 0) == 1.0)
check("强度 200 更强（取平方）", abs(pool.recency_weight(1, 2.0) - 0.0625) < 1e-9)
check("强度只影响折扣、不归零", all(pool.recency_weight(d, 2.0) > 0 for d in range(0, 30)))

ws = pool.recency_weights(C, [(TODAY, "c3"), ("2026-11-14", "c7"), ("2026-11-13", "c9")],
                          TODAY)
check("recency_weights 命中项被打折、其余为 1",
      abs(ws[3] - 0.15) < 1e-9 and abs(ws[7] - 0.25) < 1e-9
      and abs(ws[9] - 0.40) < 1e-9 and ws[0] == 1.0)
check("同角色多次命中取最近一次",
      abs(pool.recency_weights(C, [(TODAY, "c3"), ("2026-11-10", "c3")], TODAY)[3] - 0.15) < 1e-9)

# 昨天抽过 c3：c3 概率应显著低于 1/40，但不为 0
weights = pool.recency_weights(C, [("2026-11-14", "c3")], TODAY)
cnt = collections.Counter()
for i in range(60000):
    seed = pool.derive_seed(TODAY, f"v{i}", "salt", 0, 0)
    cnt[pool.pick_character_weighted(seed, C, weights)["id"]] += 1
p_hit = cnt["c3"] / 60000
p_base = sum(1 for v in cnt.values() if v) / 60000  # 占位，避免未使用
check("昨天抽过的角色概率下降但仍可抽到",
      p_hit < (1 / 40) * 0.5 and cnt["c3"] > 0,
      f"实测 {p_hit * 100:.3f}%（基线 {100 / 40:.2f}%）")

# 长期「连续两天同角色」比例：基线 1/30，降权后应明显更低
C30 = cands(30)
same, total = 0, 0
for u in range(2000):
    hist, prev = [], ""
    for day in range(1, 21):
        date = f"2026-10-{day:02d}"
        seed = pool.derive_seed(date, f"long{u}", "salt", 0, 0)
        w = pool.recency_weights(C30, hist, date, 1.0)
        e = pool.pick_character_weighted(seed, C30, w)["id"]
        if prev:
            total += 1
            same += 1 if e == prev else 0
        hist.append((date, e))
        prev = e
ratio = same / total if total else 1.0
check("长期「连续两天同角色」明显低于基线（1/30）", ratio < (1 / 30) * 0.6,
      f"实测 {ratio * 100:.2f}% vs 基线 {100 / 30:.2f}%")

# ---------- 3) 立绘轮回 ----------
files = [f"portraits/p{i}.png" for i in range(5)]
used, got = [], []
for i in range(5):
    seed = pool.derive_seed(TODAY, f"cyc{i}", "salt", 0, 0)
    f, used_n, new_round = pool.portrait_cycle_pick(files, used, seed)
    got.append(f)
    used.append(f)
check("5 张立绘连续抽 5 次全不相同", len(set(got)) == 5, str(got))

# 第 6 次：一轮已凑齐 → 进入新一轮，且不与第 5 张重复
seed = pool.derive_seed(TODAY, "cyc6", "salt", 0, 0)
f6, used_n6, new_round6 = pool.portrait_cycle_pick(files, used, seed)
check("抽完一轮进入新一轮（不撞上一轮最后一张）",
      new_round6 and f6 in files and f6 != got[-1], f"{f6} / last={got[-1]}")

# 一轮中途补进新图：新图应进入剩余（构造一个必然选到它的场景）
pool4 = files[:3]
used2 = [pool4[0], pool4[1]]          # 已用 2 张，剩 1 张 + 新补 1 张
newfile = "portraits/new.png"
picks = set()
for i in range(60):
    seed = pool.derive_seed(TODAY, f"ins{i}", "salt", 0, 0)
    picks.add(pool.portrait_cycle_pick(pool4 + [newfile], used2, seed)[0])
check("补进新图后会被轮流到", newfile in picks and len(picks) == 2, str(picks))

check("池里只有 1 张时永远返回它（不炸）",
      pool.portrait_cycle_pick(["only.png"], ["only.png"], b"x" * 32)[0] == "only.png")
check("空池返回空串", pool.portrait_cycle_pick([], [], b"x" * 32)[0] == "")
check("历史里出现已删除的图不影响判断",
      pool.portrait_cycle_pick(files[:2], ["portraits/gone.png"], b"y" * 32)[0] in files[:2])

# ---------- 4) 流水读取（真 SQL） ----------
import store  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    st = store.Store(Path(td) / "waifu.db")
    st.upsert_draw("u1", "2026-11-13", "c1", 0, False, char_name="甲",
                   portrait_file="portraits/a.png")
    st.upsert_draw("u1", "2026-11-14", "c2", 0, False, char_name="乙",
                   portrait_file="portraits/b.png")
    st.upsert_draw("u1", "2026-11-14", "c2", 1, True, char_name="乙",
                   portrait_file="portraits/c.png")
    st.upsert_draw("u2", "2026-11-14", "c9", 0, False, char_name="别人")
    hist = st.draw_history("u1")
    check("draw_history 只取本人、旧→新",
          [h["char_id"] for h in hist] == ["c1", "c2", "c2"]
          and [h["portrait_file"] for h in hist][-1] == "portraits/c.png")
    check("draw_history 保留换老婆的两次签", [h["nonce"] for h in hist] == [0, 0, 1])
    check("draw_history 未收录别人的签", all(h["char_id"] != "c9" for h in hist))
    st.close()

if FAILURES:
    print(f"test_fairness: {len(FAILURES)} FAILED")
    sys.exit(1)
print("test_fairness: ALL OK")
