# -*- coding: utf-8 -*-
"""batch_fill.py 纯函数自检（可直接运行，退出码即结论；不联网、不依赖 astrbot）。

守卫点：主题轮换（相邻不重复、分布不退化、分层配比 / 避历史 / 续补不重放）、
前提筛选（配额不足 + 参考图开关）、任务展开（逐张带主题）、
批量条目画法纠正（有参考图强制 img2img）、BatchRunner 状态机。
"""
import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import batch_fill as bf  # noqa: E402


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL: {name} {detail}")
        sys.exit(1)
    print(f"ok: {name}")


def main():
    ids = bf.content_theme_ids()
    check("内容主题 >= 10 且不含 random/tpl_",
          len(ids) >= 10 and "random" not in ids
          and not any(str(t).startswith("tpl_") for t in ids))

    plan = bf.theme_plan("c1", "salt", 60)
    check("主题序列长度正确", len(plan) == 60)
    check("相邻主题不重复", all(a != b for a, b in zip(plan, plan[1:])))
    import collections
    cnt = collections.Counter(plan)
    check("60 张覆盖 >= 6 个主题（分布不退化）", len(cnt) >= 6, str(cnt))
    check("主题序列决定论", bf.theme_plan("c1", "salt", 60) == plan)
    check("不同角色序列不同", bf.theme_plan("c2", "salt", 10) != plan[:10])
    check("n=0 安全", bf.theme_plan("x", "salt", 0) == []
          and bf.theme_plan("x", "salt", -3) == [])

    # v0.22.4：主题计划重做——内容主题为主、一轮不重复、避开历史、续补不重放
    p5 = bf.theme_plan("c1", "salt", 5, date="2026-11-13")
    content_n = sum(1 for t in p5 if not str(t).startswith("art_"))
    check("5 张里内容主题 >= 3（画风主题不再占多数）", content_n >= 3, str(p5))
    check("一轮内主题不重复", len(set(p5)) == 5, str(p5))
    check("跨角色 5 张序列不同",
          bf.theme_plan("c2", "salt", 5, date="2026-11-13") != p5)
    p_avoid = bf.theme_plan("c1", "salt", 3, have=3, date="2026-11-13",
                            used=p5[:3])
    check("续补避开已用主题", not (set(p_avoid) & set(p5[:3])), str(p_avoid))
    check("have/date 参与洗牌（续补不重放开头）",
          bf.theme_plan("c1", "salt", 5, have=3, date="2026-11-13") != p5
          and bf.theme_plan("c1", "salt", 5, date="2026-11-14") != p5)
    check("新计划仍是决定论",
          bf.theme_plan("c1", "salt", 5, date="2026-11-13") == p5)

    # v0.19.0：纯画风主题（art_*）也要参与批量补图轮换——否则「一批图换个画风」
    # 只能一张张手点，用户反馈画风太单一的诉求在批量场景下没被解决
    art_ids = [t for t in ids if str(t).startswith("art_")]
    check("批量轮换池含纯画风主题（>= 8）", len(art_ids) >= 8, str(art_ids))
    wide = bf.theme_plan("c1", "salt", 120)
    check("120 张里出现过纯画风主题",
          any(str(t).startswith("art_") for t in wide))

    entries = [dict(id="a"), dict(id="b"), dict(id="c"), dict(id="d")]
    counts = {"a": 0, "b": 5, "c": 3}
    has_ref = lambda e: e["id"] in ("a", "c")
    cands = bf.pick_candidates(entries, lambda cid: counts.get(cid, 0), 5, True, has_ref)
    check("前提筛选：缺图且无参考图跳过",
          [c["entry"]["id"] for c in cands] == ["a", "c"])
    check("缺几张补几张", cands[0]["need"] == 5 and cands[1]["need"] == 2)
    cands2 = bf.pick_candidates(entries, lambda cid: counts.get(cid, 0), 5, False,
                                lambda e: False)
    check("require_ref=false 时 base 条目也入列",
          [c["entry"]["id"] for c in cands2] == ["a", "c", "d"])
    check("满配额角色不入列", all(c["entry"]["id"] != "b" for c in cands2))

    tasks = bf.build_tasks(cands, "salt")
    check("任务展开 = Σneed", len(tasks) == 7)
    check("每张任务带池内主题", all(t["theme_id"] in ids for t in tasks))
    a_plan = [t["theme_id"] for t in tasks if t["char_id"] == "a"]
    check("角色内相邻任务主题不重复",
          all(x != y for x, y in zip(a_plan, a_plan[1:])))

    # BatchRunner：状态机 + 停止语义（假 draw/prompt_gen 桩，不联网）
    runner = bf.BatchRunner()
    snap = runner.snapshot()
    check("初始快照", snap["running"] is False and snap["total"] == 0)

    class FakeStore:
        def __init__(self):
            self.rows = []

        def next_portrait_seq(self, cid):
            return 0

        def alloc_portrait_seq(self, cid):
            return 0

        def add_portrait(self, *a, **k):
            self.rows.append((a, k))

    class FakePlugin:
        _today = staticmethod(lambda: "2026-11-13")
        _cfg = staticmethod(lambda k, d=None: {"salt": "s", "draw_timeout": 1}.get(k, d))
        context = object()
        _store = FakeStore()
        _data_dir = Path(".")

        def _draw_cfg(self):
            return {}

    # v0.22.4：挂了参考图的 base 角色必须纠正成 img2img（趁 draw 还是真模块测）
    with tempfile.TemporaryDirectory() as td:
        ref = Path(td) / "ref.png"
        ref.write_bytes(b"x")
        base_e = {"id": "x", "strategy_type": "base",
                  "strategy": {"type": "base", "ref": str(ref), "denoise": 0.4}}
        eff = bf.effective_batch_entry(base_e, td)
        check("有参考图的 base 角色纠正为 img2img",
              eff["strategy_type"] == "img2img"
              and eff["strategy"]["ref"] == str(ref)
              and eff["strategy"]["denoise"] == 0.4)
        check("纠正不改原条目", base_e["strategy_type"] == "base")
        no_ref = {"id": "y", "strategy_type": "base", "strategy": {"type": "base"}}
        check("无参考图角色保持原画法",
              bf.effective_batch_entry(no_ref, td)["strategy_type"] == "base")
        i2i = dict(base_e, strategy_type="img2img")
        check("已是 img2img 原样返回",
              bf.effective_batch_entry(i2i, td) is i2i)

    import types
    real_draw, real_pg, real_pool = bf.draw, bf.prompt_gen, bf.pool
    fake_draw = types.SimpleNamespace(
        generate_portrait=async_init(lambda *a, **k: ("/tmp/x.png", "ok", "", 1)),
        archive_portrait=lambda *a: "portraits/x/1.png",
        image_size=lambda *a: (0, 0),  # v0.20.0：随档尺寸（桩不给真值）
        has_gen_refs=lambda e, d=None: False,   # v0.22.4：队列桩无参考图
        effective_entry=lambda e, o: e)
    fake_prompt_gen = types.SimpleNamespace(
        THEMES=real_pg.THEMES,
        build_generation_prompt=async_init(lambda *a, **k: ("p", "local")),
        describe_variations=lambda *a, **k: [])  # v0.20.0：随档维度（桩不给真值）
    fake_pool = types.SimpleNamespace(
        portrait_seed_int=lambda *a: 1)

    real_draw, real_pg, real_pool = bf.draw, bf.prompt_gen, bf.pool
    bf.draw = fake_draw
    bf.prompt_gen = fake_prompt_gen
    bf.pool = fake_pool
    try:
        tasks = bf.build_tasks(cands, "salt")

        async def run_all():
            n = runner.start(FakePlugin(), tasks)
            check("队列启动返回任务数", n == 7)
            for _ in range(100):
                await asyncio.sleep(0.02)
                if not runner.snapshot()["running"]:
                    break

        asyncio.run(run_all())
        snap = runner.snapshot()
        check("全部完成", snap["done"] == 7 and not snap["running"])
        check("成功计数", snap["ok"] == 7 and snap["failed"] == 0)
        check("逐张带主题与状态",
              all(it.get("status") == "ok" and it.get("theme_label") for it in snap["items"]))
        check("随档记录 prompt/theme", len(FakePlugin._store.rows) == 7
              and all("theme" in k for _a, k in FakePlugin._store.rows))
        # 停止语义：start 后立刻 stop → 不再开始新任务
        runner2 = bf.BatchRunner()

        class SlowDraw:
            @staticmethod
            async def generate_portrait(*a, **k):
                await asyncio.sleep(0.05)
                return ("/tmp/x.png", "ok", "", 1)

            archive_portrait = staticmethod(lambda *a: "p")
        bf.draw = SlowDraw
        bf.prompt_gen = fake_prompt_gen
        async def run_stop():
            runner2.start(FakePlugin(), tasks)
            runner2.stop()
            for _ in range(150):
                await asyncio.sleep(0.02)
                if not runner2.snapshot()["running"]:
                    break

        asyncio.run(run_stop())
        snap2 = runner2.snapshot()
        check("停止后 done < total（未完成部分不再开始）",
              snap2["done"] < 7 and snap2["stopped"], f"done={snap2['done']}")
    finally:
        bf.draw, bf.prompt_gen, bf.pool = real_draw, real_pg, real_pool


def async_init(fn):
    """把同步函数包装成 async 函数桩。"""

    async def wrapper(*a, **k):
        return fn(*a, **k)

    return wrapper


if __name__ == "__main__":
    main()
