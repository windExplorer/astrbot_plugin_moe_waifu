# -*- coding: utf-8 -*-
"""萌绘忙排队等待守卫（v0.66.0，可直接运行，退出码即结论）。

用户口径：萌绘正在画图时本插件不应直接报错，应加入队列等待萌绘空闲再提交。
实现 = draw.generate_portrait 提交前 wait_anima_idle 轮询萌绘本地队列
（_server_pending，防御性反射），等待不占 draw_timeout、不计耗时统计。
守卫点：
  ① anima_queue_ahead 反射：正常 / 萌绘未装 / 解析异常均不抛，未知返回 -1；
  ② wait_anima_idle：忙→空闲返回等待说明并回调 progress；budget=0 直接跳过；
  ③ 预算耗尽仍忙 → 返回超时说明（提交兜底，不抛异常）；
  ④ generate_portrait 在计时起点（t0）之前 await wait_anima_idle——
     等待既不占 draw_timeout 也不进耗时统计（源码级守卫）。
"""
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import draw  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if not cond:
        FAILURES.append(name)
        print(f"FAIL: {name} {detail}")
    else:
        print(f"ok: {name}")


def make_anima(pending):
    """最小萌绘桩：与真实现同名的解析 / 队列结构。"""
    server = {"name": "local", "url": "http://127.0.0.1:8188"}

    def resolve_wf(name=None, is_img2img=False):
        return {"name": name or "默认工作流", "server_key": "local"}

    def resolve_srv(ref=None):
        return server

    return SimpleNamespace(_resolve_workflow=resolve_wf,
                           _resolve_server=resolve_srv,
                           _server_key=lambda s: s["name"],
                           _server_pending={"local": list(pending)})


def make_context(anima):
    ctx = SimpleNamespace()
    if anima is None:
        def _boom(name):
            raise RuntimeError("not installed")
        ctx.get_registered_star = _boom
    else:
        ctx.get_registered_star = lambda name: SimpleNamespace(star_cls=anima)
    return ctx


def test_queue_ahead_reflection():
    ctx = make_context(make_anima(["p1", "p2"]))
    check("有 2 个待处理任务返回 2",
          draw.anima_queue_ahead(ctx, "", "", False) == 2)
    check("空队列返回 0（空闲）",
          draw.anima_queue_ahead(make_context(make_anima([])), "", "", False) == 0)
    check("萌绘未装返回 -1", draw.anima_queue_ahead(make_context(None)) == -1)

    broken = SimpleNamespace(_resolve_workflow=None, _resolve_server=None,
                             _server_key=None, _server_pending=None)
    check("结构缺失返回 -1", draw.anima_queue_ahead(make_context(broken)) == -1)

    def boom(name=None, is_img2img=False):
        raise ValueError("未配置任何工作流")
    half = make_anima([])
    half._resolve_workflow = boom
    check("工作流解析异常返回 -1", draw.anima_queue_ahead(make_context(half)) == -1)


def test_wait_idle_flow():
    # 忙 → 空闲：第一轮读到 1 个待处理，第二轮空闲
    state = {"calls": 0}

    def ahead(ctx, wf="", iwf="", i2i=False):
        state["calls"] += 1
        return 1 if state["calls"] == 1 else 0

    real = draw.anima_queue_ahead
    draw.anima_queue_ahead = ahead
    try:
        notes = []
        t0 = time.monotonic()
        note = asyncio.run(draw.wait_anima_idle(
            None, {"anima_queue_wait": 30}, "comfyui_draw", {},
            progress=notes.append))
        cost = time.monotonic() - t0
        check("忙→空闲返回等待说明", "排队等待空闲" in note and "前面 1 个" in note, note)
        check("progress 收到同一条说明", len(notes) == 1 and "排队等待空闲" in notes[0],
              str(notes))
        check("只等了一轮轮询（约 3s），不是预算耗尽", cost < 10, f"{cost:.1f}s")
    finally:
        draw.anima_queue_ahead = real

    # 本来就空闲：立即返回空串，不回调
    draw.anima_queue_ahead = lambda *a, **k: 0
    try:
        notes = []
        note = asyncio.run(draw.wait_anima_idle(
            None, {"anima_queue_wait": 30}, "comfyui_draw", {},
            progress=notes.append))
        check("空闲直接提交（无说明）", note == "" and notes == [], f"{note}/{notes}")
    finally:
        draw.anima_queue_ahead = real


def test_wait_disabled_and_timeout():
    # budget=0：预等待关闭，直接提交（旧行为）
    def never_called(*a, **k):
        raise AssertionError("关闭预等待时不应探测队列")
    real = draw.anima_queue_ahead
    draw.anima_queue_ahead = never_called
    try:
        note = asyncio.run(draw.wait_anima_idle(
            None, {"anima_queue_wait": 0}, "comfyui_draw", {}, progress=None))
        check("budget=0 跳过预等待", note == "", note)
    finally:
        draw.anima_queue_ahead = real

    # 预算耗尽仍忙：返回超时说明并照常提交（不抛异常）
    draw.anima_queue_ahead = lambda *a, **k: 3
    try:
        t0 = time.monotonic()
        note = asyncio.run(draw.wait_anima_idle(
            None, {"anima_queue_wait": 1}, "comfyui_draw", {}, progress=None))
        cost = time.monotonic() - t0
        check("预算耗尽返回超时说明", "超时" in note and "仍尝试提交" in note, note)
        check("超时按预算收敛（不卡满轮询间隔）", cost < 8, f"{cost:.1f}s")
    finally:
        draw.anima_queue_ahead = real


def test_generate_portrait_wires_wait_before_t0():
    """源码级守卫：等待必须在计时起点之前（不占 draw_timeout / 不进耗时统计）。"""
    src = (ROOT / "draw.py").read_text(encoding="utf-8")
    wait_at = src.find("await wait_anima_idle(context, cfg, tool_name, kwargs, progress)")
    t0_at = src.find("t0 = time.monotonic()")
    check("generate_portrait 内有预等待调用", wait_at > 0)
    check("预等待在计时起点之前", 0 < wait_at < t0_at,
          f"wait@{wait_at} t0@{t0_at}")
    check("generate_portrait 暴露 progress 参数", "progress=None" in src.split(
        "async def generate_portrait", 1)[1].split('"""', 2)[0])


def main():
    jobs = [("queue_ahead", test_queue_ahead_reflection),
            ("wait_idle_flow", test_wait_idle_flow),
            ("disabled_and_timeout", test_wait_disabled_and_timeout),
            ("wired_before_t0", test_generate_portrait_wires_wait_before_t0)]
    for name, fn in jobs:
        print(f"--- {name} ---")
        fn()
    if FAILURES:
        print(f"test_anima_queue_wait: {len(FAILURES)} FAILED")
        sys.exit(1)
    print("test_anima_queue_wait: ALL OK")


if __name__ == "__main__":
    main()
