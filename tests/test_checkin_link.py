# -*- coding: utf-8 -*-
"""签到联动守卫（可直接运行，退出码即结论）。

口径（v0.55.0）：
- 「签到」由本插件当编排者：抢在 get_px 之前建后台任务，先抽老婆、再叫星语；
- 入口 handler 必须 priority > 0（get_px 的 /签到 是 0，它第一行就 stop_event，
  排在它后面就永远不会执行），且**绝不能 stop_event / yield**，否则它自己的
  签到卡会被我们拦掉；
- 只做当日首签：当天已抽过 / 正在抽的一律静默跳过（用户手发原指令不受影响）。
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWED = {"int", "float", "bool", "string", "text", "list", "file",
           "object", "template_list", "dict"}


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL: {name} {detail}")
        sys.exit(1)
    print(f"ok: {name}")


src = (ROOT / "main.py").read_text(encoding="utf-8")
schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))


def body_of(name: str) -> str:
    """取方法体源码（到下一个同级 def 为止），用于静态守卫。"""
    start = src.index(f"def {name}(")
    rest = src[start + 10:]
    m = re.search(r"\n    (?:@|async def |def )", rest)
    return rest[: m.start()] if m else rest


# ---------- 配置 ----------
keys = {"checkin_link_enabled", "checkin_link_only_first",
        "checkin_link_include_star", "checkin_link_require_wake",
        "checkin_link_delay"}
check("签到联动配置键齐全", keys.issubset(schema.keys()),
      f"缺：{sorted(keys - set(schema.keys()))}")
bad = [k for k in keys if schema.get(k, {}).get("type") not in ALLOWED]
check("签到联动配置 type 合法", not bad, f"非法：{bad}")
check("签到联动默认关闭", schema["checkin_link_enabled"].get("default") is False)
check("默认仅当日首签", schema["checkin_link_only_first"].get("default") is True)
check("默认顺带触发星语", schema["checkin_link_include_star"].get("default") is True)

# ---------- 入口 ----------
entry = body_of("checkin_link_entry")
check("注册「签到」入口", '@filter.regex(CHECKIN_LINK_RE, priority=999)' in src)
check("入口优先级最高（999，抢在 get_px 之前）", "priority=999" in src)
# /签到 必须能触发联动（裸词与带唤醒前缀都认）
check("正则同时接受 /签到 与裸词", "CHECKIN_LINK_RE = r\"^/?签到$\"" in src)
_pat = re.compile(r"^/?签到$")
check("裸词匹配", bool(_pat.search("签到")))
check("斜杠命令匹配", bool(_pat.search("/签到")))
check("其它指令不匹配", not _pat.search("/今日老婆") and not _pat.search("签到啦"))
check("入口不吞事件", "stop_event" not in entry)
check("入口是普通协程（不 yield）", "yield" not in entry)
check("入口只建后台任务", "create_task" in entry)
check("入口挡重复触发", "CHECKIN_LINK_EXTRA" in entry)
check("入口掐掉 LLM 兜底", "should_call_llm(False)" in entry)
check("入口处理唤醒前缀开关", "checkin_link_require_wake" in entry)

# ---------- 编排顺序 ----------
run = body_of("_run_checkin_link")
check("先抽老婆", run.index("_linked_waifu") < run.index("_linked_star"))
check("星语可关闭", "checkin_link_include_star" in run)

# ---------- 首签判定 ----------
waifu_link = body_of("_linked_waifu")
check("老婆侧查当天是否已抽", "get_draw(" in waifu_link)
check("老婆侧查是否正在抽", "_inflight" in waifu_link)
check("老婆侧受 only_first 控制", "checkin_link_only_first" in waifu_link)
check("老婆侧沿用群/私聊守卫",
      "_disabled_groups()" in waifu_link and "_allow_private()" in waifu_link)

# ---------- 跨插件调用 ----------
star_link = body_of("_linked_star")
check("星语按插件名取实例", "get_registered_star" in star_link)
check("星语未启用时跳过", "activated" in star_link)
check("星语旧版本安全降级", "linked_fortune" in star_link)

# ---------- 发送与生命周期 ----------
check("联动结果走 context 主动发送", "context.send_message" in body_of("_send_linked_result"))
check("terminate 取消联动任务", "link_tasks" in body_of("terminate"))

print("\nRESULT: 签到联动守卫全部通过")
