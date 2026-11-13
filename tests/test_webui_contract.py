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
movePortraitToRef moveRefToPortrait doQuickAction
galOrphans bindOrphan galOrphansOpen deleteOrphan
ov goMenu refreshing refreshAll
galDrawRef galDrawRefAbs setDrawSource galRefsSel
workKeyword workFilterCat workFilterVerified workSort workCatOpts workVerifiedOpts
workSortOpts filteredWorks toggleWorkChar openNewForWork
fmtTime cfgBoundGroups pickSimGroup simScopeText
avatarOf hashColor thumbOf colorOf rerollLabel tv brandColor zhLocale
bindRows bindSeriesOpts bindRawOpen bindLoading loadBindPools onBindRawInput
addBindRow setRowGid setRowSeries removeBindRow
galWidth galHeight
importOverwrite dedupeOpen dedupeGroups dedupeSelected dedupeBusy dedupeResult
dedupeSelectedCount dedupeDupCount openDedupe loadDuplicates doDedupe
""".split()

ROUTES = """
/records /records/detail /records/events /stats /users /user /user/avatar /filters
/debug/state /debug/history /debug/simulate /debug/rerender
/portraits/move_to_ref /refs/move_to_portrait /refs/orphans
/refs/orphan_delete
/portraits/restore /portraits/delete_hard
/refs/set_draw_source /overview
/prompt/pools
/batch/preview /batch/start /batch/status /batch/stop
/works/discover /works/discover_add
/pools/import /pools/duplicates /pools/dedupe
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
          front_keys == back_keys and len(front_keys) == 10
          and front_keys[0] == "overview",
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

    # 6) 星语式调试交互：重置类按钮只在「查到状态」之后出现，且高级区不再常驻
    gate = page.count('v-if="dbgState"')
    check("重置 / 重绘 / 高级区都受 dbgState 约束", gate >= 4, f"实际 {gate} 处")
    check("旧常驻危险操作区已改为上下文式",
          'title="高级 · 指定任意目标（两段确认）"' in page
          and 'title="危险操作 · 重置（两段确认）"' not in page)
    check("快捷操作卡带两段确认", "quick:clear_today" in page and "quick:reroll" in page)

    # 7) 时间显示：库里是 UTC ISO，UI 必须按配置时区格式化，不能裸显 ...Z
    raw_ts = re.findall(r"\{\{[^}]*?\b(?:created_at|updated_at)\s*\}\}", template)
    check("时间字段全部经 fmtTime 格式化", not raw_ts, str(raw_ts[:3]))
    check("时区来自插件配置", '"timezone": str(plugin._cfg' in api
          and "cfgTimezone.value" in page)

    # 8) 群号：规则统一说明 + 查状态自动带出 + 模拟结果说清实际抽取范围
    check("群号抽取规则在多个页面统一提示",
          page.count("该群绑定作品") >= 3, f"实际 {page.count('该群绑定作品')} 处")
    check("已绑定群来自配置、可一键填入", '"bound_groups"' in api
          and "cfgBoundGroups" in page and "pickSimGroup" in page)
    check("查状态自动带出群号", "simF.value.group_id = res.draw.group_id" in page)
    check("模拟抽签返回抽取范围", '"scope": scope' in api and "simScopeText" in page)

    # 9) 刷新链路（v0.10.1）：封面缩略在响应顶层 thumbs/cover_paths 字典里，
    #    条目本身没有 thumb；写操作必须走统一 refreshAll，否则「设了封面
    #    作品库/角色卡还是旧图」会复发。
    check("allEntries 合并后端 thumbs/cover_paths",
          "meta.value.thumbs" in page and "meta.value.cover_paths" in page)
    check("统一刷新入口覆盖主要快照",
          all(name in page for name in
              ("async function refreshAll", "loadWorks()", "loadGallery()",
               "loadGalRefs()", "loadOverview()")))
    for fn in ("setPortraitCover", "setWorkCover", "doArchive", "doTryToRef",
               "doDeletePortrait", "movePortraitToRef", "moveRefToPortrait"):
        body = re.search(
            r"(?:async )?function " + fn + r"\([^)]*\)\s*\{(.*?)\n      \}",
            page, re.S)
        check(f"{fn} 后走统一刷新",
              bool(body) and "refreshAll(" in body.group(1),
              "函数体未找到 refreshAll")
    check("切页签补了 chars/works/try/bind/config 懒刷新",
          all(f'm === "{k}"' in page for k in
              ("chars", "works", "try", "bind", "config")))

    # 10) 交互口径（v0.12.0 建立、v0.15.0 演进）：立绘默认最新在前=galItemsView
    #     排序收口（newest 默认值，seq 降序 + 归档时间决胜）；参考图列表仍走反转；
    #     出卡源用卡片按钮指定（不再用路径下拉）
    check("立绘默认最新在前（galItemsView 收口）",
          'galSort = Vue.ref("newest")' in page and "galItemsView" in page
          and 'v-for="it in galItemsView"' in page)
    check("参考图列表最新在前保留", "galRefs.value = ((res && res.items) || []).slice().reverse();" in page)
    check("出卡源由卡片按钮指定",
          'setDrawSource(it.abs)' in page and 'setDrawSource(r.path)' in page)
    check("出卡源不再用路径下拉", "galDrawOpts" not in page)
    check("作品库有筛选 / 排序 / 添加角色（新建）/ 启停",
          all(k in page for k in ("filteredWorks", "workSort", "openNewForWork",
                                  "toggleWorkChar")))

    # 11) 渲染健壮性（v0.13.5）：库里 seed 是 INTEGER，模板对数字直接 .slice
    #     会抛 TypeError → 整个立绘墙渲染中断（真机「只有标题没图片」事故）
    check("立绘卡 seed 截断先 String 化（数字无 .slice，直接 slice 炸渲染）",
          "String(it.seed" in template
          and '(it.seed || "").slice' not in template)

    # 12) 危险操作分域（v0.19.5 事故）：调试页「清空老婆立绘库」作用对象是**角色**，
    #     却被放在「今日签操作（某个 QQ）」那一排，用户误点后该角色的立绘库（所有用户
    #     共用）被清空且文件被 unlink 无法恢复。三条守卫：
    #     ① 清角色立绘库必须服务端 confirm；② 破坏性删除走回收站（shutil.move）；
    #     ③ 前端把「按角色清库」与「按用户清签」拆成两个入口。
    check("清角色立绘库需服务端 confirm",
          'body.get("confirm")' in api and "confirm: true" in page)
    check("破坏性删除走回收站（_trash_files + shutil.move）",
          "_trash_files" in api and "shutil.move" in api and "_trash" in page)
    check("前端按角色清库独立入口", "doClearCharPortraits" in page
          and "按角色清库（危险 · 与 QQ 用户无关）" in page)
    check("前端有按用户清空全部签", "clear_all_draws" in page)
    check("clear_portraits 不再挂在今日签快捷操作里",
          "doQuickAction('clear_portraits')" not in page)

    if FAILURES:
        print(f"test_webui_contract: {len(FAILURES)} FAILED")
        sys.exit(1)
    print("test_webui_contract: ALL OK")


if __name__ == "__main__":
    main()
