# -*- coding: utf-8 -*-
"""WebUI 契约守卫（可直接运行，退出码即结论）。

盯住四处最容易「改一处漏一处」的漂移：
  1) 前端 menuOpts 的 key ↔ 后端 _UI_MENUS 页签记忆白名单；
  2) 模板用到的新页签变量 ↔ setup return 暴露清单；
  3) 新数据路由 ↔ webui_api.register 路由表；
  4) 新 store 方法 ↔ 数据层真实定义。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "pages" / "waifu-console" / "index.html"
API = ROOT / "webui_api.py"
STORE = ROOT / "store.py"

FAILURES = []


def check(name, cond, detail=""):
    if not cond:
        FAILURES.append(name)
        print(f"FAIL: {name} {detail}")
    else:
        print(f"ok: {name}")


# v0.9.0 四个新页签在模板里引用的顶层变量 / 函数
BINDINGS = """
recF recRows recTotal recPage recSize recLoading groupOpts charSelectOpts platformOpts
rerollOpts eventKindOpts rangeQuickOpts loadRecords setRecPage setRecRange
resetRecFilters openRecDetail gotoUser recDetailOpen recDetail recEvents recEventKind
loadRecEvents recJobs recJobsTotal recJobsLoading recJobStatus jobStatusOpts loadRecJobs
statsDays statsRangeOpts statsData statsLoading statCards trendChart hoursChart
groupDonut donutColors statsMaxChar statsMaxDecision loadStats setStatsDays
userQuery userOrder userOrderOpts userRows userTotal userPage userSize usersLoading
userSelected userDetail userDetailLoading userStatCards userMaxChar
userCalendarMonths weekLabels loadUsers setUserPage openUser
dbgStateUid dbgState dbgStateLoading dbgRecent dbgAudit dbgConfigRows
loadDebugState loadDebugHistory pickRecentUser simF simResult simBusy
doSimulate simulateNext dbgRenderUid dbgRenderDate doRerender
dbgUid dbgDate dbgUid2 dbgDate2 dbgChar dbgChar2 dbgBusy
doForceReroll doClearDraws doResetCharDay doClearCharPortraits
charNameOf decisionLabel barPct
movePortraitToRef moveRefToPortrait
""".split()

ROUTES = """
/records /records/detail /records/events /stats /users /user /filters
/debug/state /debug/history /debug/simulate /debug/rerender
/portraits/move_to_ref /refs/move_to_portrait
""".split()

STORE_METHODS = """
_migrate upsert_draw update_draw_card list_records record_detail list_events
list_groups list_platforms list_users user_profile stats_range
debug_log debug_recent debug_audit
""".split()


def main():
    page = PAGE.read_text(encoding="utf-8")
    api = API.read_text(encoding="utf-8")
    store = STORE.read_text(encoding="utf-8")

    # 1) 页签白名单一致
    menu_block = re.search(r"var menuOpts = \[(.*?)\];", page, re.S)
    check("页面存在 menuOpts", menu_block is not None)
    front_keys = re.findall(r'key:\s*"(\w+)"', menu_block.group(1)) if menu_block else []
    ui_block = re.search(r"_UI_MENUS = \((.*?)\)", api, re.S)
    check("后端存在 _UI_MENUS", ui_block is not None)
    back_keys = re.findall(r'"(\w+)"', ui_block.group(1)) if ui_block else []
    check("页签前后一致（页签记忆不会写入被拒）",
          front_keys == back_keys and len(front_keys) == 9,
          f"front={front_keys} back={back_keys}")
    check("四个新页签齐全",
          {"records", "stats", "users", "debug"} <= set(front_keys))

    # 2) setup return 暴露清单
    script = re.findall(r"<script>(.*?)</script>", page, re.S)
    check("主内联脚本恰好一段", len(script) == 1, f"实际 {len(script)}")
    main_script = script[0] if script else ""
    returns = list(re.finditer(r"return \{(.*?)\n\s*\};", main_script, re.S))
    check("能定位 setup 的 return 块", bool(returns))
    exposed = set(re.findall(r"\b(\w+)\s*:", returns[-1].group(1))) if returns else set()
    missing = [b for b in BINDINGS if b not in exposed]
    check("新页签变量全部已暴露给模板", not missing, f"缺：{missing}")
    template = page.split('<script src="assets/vue.global.prod.js">')[0]
    unused = [b for b in BINDINGS if not re.search(r"\b" + re.escape(b) + r"\b", template)]
    check("暴露的契约键确实被模板用到（防清单腐化）", not unused, f"未使用：{unused}")

    # 3) 路由表
    register = api.split("def register(plugin)")[1]
    absent = [r for r in ROUTES if f'"{r}"' not in register]
    check("新数据路由全部已注册", not absent, f"缺：{absent}")

    # 4) 数据层方法
    absent_methods = [m for m in STORE_METHODS if f"def {m}(" not in store]
    check("新 store 方法全部存在", not absent_methods, f"缺：{absent_methods}")

    # 5) 老库可升级：迁移必须靠 ALTER，不能重建表
    check("迁移用 PRAGMA 探测 + ALTER（不重建表）",
          "PRAGMA table_info" in store and "ALTER TABLE" in store)

    if FAILURES:
        print(f"test_webui_contract: {len(FAILURES)} FAILED")
        sys.exit(1)
    print("test_webui_contract: ALL OK")


if __name__ == "__main__":
    main()
