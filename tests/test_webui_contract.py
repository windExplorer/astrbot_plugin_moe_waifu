# -*- coding: utf-8 -*-
"""WebUI 契约守卫（可直接运行，退出码即结论）。

盯住四处最容易「改一处漏一处」的漂移：
  1) 前端 menuOpts 的 key ↔ 后端 _UI_MENUS 页签记忆白名单；
  2) 模板用到的新页签变量 ↔ setup return 暴露清单；
  3) 新数据路由 ↔ webui_api.register 路由表；
  4) 新 store 方法 ↔ 数据层真实定义。
"""
import json
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
doClearCharPortraitsQuick
charNameOf decisionLabel barPct
movePortraitToRef moveRefToPortrait doQuickAction
galOrphans bindOrphan galOrphansOpen deleteOrphan
ov goMenu refreshing refreshAll
galDrawRef galDrawRefAbs setDrawSource galRefsSel
workKeyword workFilterCat workFilterVerified workSort workCatOpts workVerifiedOpts
workSortOpts filteredWorks toggleWorkChar openNewForWork
fmtTime cfgBoundGroups pickSimGroup simScopeText
fmtClock groupNameOf groupTitle groupAvatarOf platformLabel
trendDurChart fmtDur
galTabs galTab switchGalTab galSummary galSummaryLoading
profileFields tierOpts galTier galProfile saveGalProfile galProfileSaving
galProfileBusy galProfileMsg galProfileMsgType genGalProfile
galTaglineBusy genGalTagline galQuoteCount
galAvatarThumb galAvatarBusy galAvatarUploadOpen galAvatarNote
galAvatarPickOpen openAvatarPick doAvatarSet avatarClear doAvatarGenerate
onAvatarPanelFiles galAvBatchOpen galAvBatch startAvatarBatch stopAvatarBatch
galAvPreviewOpen galAvPreview galAvPreviewLoading openAvatarBatchPreview
galAvOverwrite setAvOverwrite
avatarThumbs avatarThumbsLoading
galFillBusy galFillLabel startGalFill stopGalFill
galFillOpen galFillRows galFillHave galFillQuota galFillLoading
galFillCount galFillNeed
rerollFillRow rerollFillAll doFillStart
galQueueTip galSubmitting
queueTopCount queuePanelOpen queueRows queueTopBroken
galArtistLoading galArtistPrompt tryArtistLoading tryArtistPrompt
ovLoading dbgAuditLoading batchArtist galFillArtist
avatarOf hashColor charAvatarOf colorOf rerollLabel tv brandColor zhLocale
bindRows bindSeriesOpts bindRawOpen bindLoading loadBindPools onBindRawInput
addBindRow setRowGid setRowSeries removeBindRow
galWidth galHeight galThemeResolved
importOverwrite dedupeOpen dedupeGroups dedupeSelected dedupeBusy dedupeResult
dedupeSelectedCount dedupeDupCount openDedupe loadDuplicates doDedupe
relStatus relBusy loadRelStatus
batchSeriesOpts batchBtnText batchRerollBusy doBatchReroll
galBulk galSel galSelCount toggleGalBulk toggleGalSel galSelAllVisible
doBulkPortrait bulkBusy bulkOffText bulkOnText
trashGroups trashBusy trashLoaded trashTotalFiles trashTotalSizeText
trashPurgeText loadTrash doTrashRestoreOne doTrashRestoreGroup
doTrashPurgeGroup doTrashPurgeAll
dbgTabs dbgTab switchDbgTab
ovVerifiedPct ovFullPct ovWithPortraitPct ovDrawSrcPct
galActiveCount configDirty scrollToConfigSection
openCharAvatarViewer openAvatarBig openImageViewerUrl
galAvatarGenOpen openAvatarGen avatarGenMode avatarGenSrc avatarGenSrcLabel
avatarGenResult avatarGenApplying applyAvatarResult discardAvatarResult
presetsBuiltin presetsCustomThemes presetsThemeRows presetsGroupLabels
presetsBusy presetsMsg presetsGroupFilter presetsKeyword presetsDim presetsNewWord
presetsEditorOpen presetsEditorIsNew presetsEditor presetsKwFields
presetsGroupOpts presetsEditorGroupOpts presetsDimOpts presetsDimWords
presetsCustomWordCount
loadPresets openPresetTheme savePresetTheme deletePresetTheme
editPresetTheme addFixedRow removeFixedRow presetsFixedRows presetsEditorTitle
toggleBuiltinTheme toggleCustomTheme
addPresetWord removePresetWord togglePresetBuiltinWord
scrollToPresetSection
helpDocs scrollToHelp gotoHelp
piOpen piTitle piMode piText piPlaceholder piFillSample piOnFile piParsing
piParse piPreview piSummaryText piBadText piDimList piDims piDimsAll
piConflict piCreate piPack piSelAll piPlanText piRows piSel piSelCount
piResult piResultText piDoImport piBusy piFocus piOnlyFocus
cdMode cdCharId cdPortrait cdPortraitOpts cdUid cdDate cdNonce cdGroup cdEnsure
cdTier cdTierOpts cdSame cdTotal cdRank cdTagline cdQuote cdHintText
cdBusy cdResult doCardDebug cdShuffle
wardrobeItems wardrobeLoading wardDeleteArm wardEditOpen wardEditRow wardEditName
wardEditCat wardEditNote wardCatOpts onWardrobeFiles beginWardrobeEdit saveWardrobeEdit
deleteWardrobe wardCharId wardCharRef wardClothes wardRefSources wardRefPickerItems
wardClothesPickerItems wardCharRefPickerOpen wardClothesPickerOpen openWardCharRefPicker
onWardCharRefConfirm onWardClothesConfirm removeWardCloth wardRefNameOf wardClothNameOf
wardScheme wardSchemeOpts wardSchemeHint wardMultiView wardDesc wardOutfitText
wardExpanding wardAiExpand wardPrompt wardRebuildPrompt wardWf wardRunning wardPreview
wardSrcPath wardError wardGenerate wardArchive
galTryons galTryonLoading galTryonCharRef galTryonClothes galTryonMultiView
galTryonDesc galTryonOutfit galTryonPrompt galTryonWf galTryonScheme
galTryonSchemeHint galTryonRunning galTryonPreview galTryonSrcPath galTryonError
galTryonGroups galTryonCharItems galTryonClothItems galTryonCharPickerOpen
galTryonClothPickerOpen galTryonEditOpen galTryonEditRow galTryonEditName
galTryonEditCat galTryonDeleteArm galTryonClothNameOf galTryonRebuild
galTryonExpand galTryonGenerate galTryonSetPortrait galTryonBeginEdit
galTryonSaveEdit galTryonDelete onGalTryonCharConfirm onGalTryonClothesConfirm
removeGalTryonCloth
""".split()

ROUTES = """
/records /records/detail /records/events /stats /users /user /user/avatar /filters
/group/avatar /groups/names
/debug/state /debug/history /debug/simulate /debug/rerender /debug/card
/portraits/move_to_ref /refs/move_to_portrait /refs/orphans
/refs/orphan_delete
/portraits/restore /portraits/delete_hard
/refs/set_draw_source /overview
/prompt/pools /prompt/artist
/batch/preview /batch/start /batch/status /batch/stop
/batch/char_start /batch/char_preview
/queue/list
/works/discover /works/discover_add
/pools/import /pools/duplicates /pools/dedupe /relational/status
/portraits/set_active /trash/list /trash/restore /trash/purge
/batch/preview /batch/reroll
/pools/quote_save /quote_check
/websearch/status
/pools/profile_save /pools/profile_gen /char/summary
/pools/profile_import/preview /pools/profile_import
/pools/avatar_set /pools/avatar_clear /avatar/generate /avatar/discard
/presets
/avatars/batch /avatars/batch_status /avatars/batch_stop
/avatars/batch_preview /avatars/thumbs
/wardrobe/list /wardrobe/upload /wardrobe/update /wardrobe/delete /wardrobe/prompt /wardrobe/expand
/tryon/list /tryon/save /tryon/update /tryon/delete /tryon/to_portrait
""".split()

STORE_METHODS = """
_migrate upsert_draw update_draw_card list_records record_detail list_events
list_groups list_platforms list_users user_profile stats_range count_events
set_group_name user_char_count today_char_stats first_event
debug_log debug_recent debug_audit debug_queries reset_char_day
add_wardrobe list_wardrobe get_wardrobe set_wardrobe_meta delete_wardrobe
add_tryon list_tryons get_tryon set_tryon_meta mark_tryon_portrait delete_tryon
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
    # v0.56.0：新增「全局图库」页签 → 13 → 14；v0.72.0：新增「衣柜」→ 15
    check("页签前后一致（页签记忆不会写入被拒）",
          front_keys == back_keys and len(front_keys) == 15
          and front_keys[0] == "overview",
          f"front={front_keys} back={back_keys}")
    # v0.46.0：卡片调试页签（模拟抽卡直接出卡）
    check("卡片调试页签已挂上（debug 之后）",
          "carddbg" in front_keys
          and front_keys.index("carddbg") == front_keys.index("debug") + 1,
          f"front={front_keys}")
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
    # 2b) **return 里的每个键都必须在脚本里真的有定义**。
    # v0.46.1 实锤：`galQuoteCount` 模板用了、return 也带了，但脚本里根本没有
    # `var` —— setup 直接 `ReferenceError: galQuoteCount is not defined`，
    # 整页白屏（还会连带报 meta 未定义）。只查「模板用到 + return 暴露」查不出这类。
    exposed_keys = re.findall(r"\b(\w+)\s*:", returns[-1].group(1)) if returns else []
    undefined = [k for k in exposed_keys
                 if not re.search(r"(?:var|let|const)\s+" + re.escape(k) + r"\s*=",
                                  main_script)
                 and not re.search(r"function\s+" + re.escape(k) + r"\s*\(",
                                   main_script)]
    check("return 的键在脚本里都有定义（防 setup ReferenceError）", not undefined,
          f"缺：{undefined}")

    # 3) 路由表
    register = api.split("def register(plugin)")[1]
    absent = [r for r in ROUTES if f'"{r}"' not in register]
    check("新数据路由全部已注册", not absent, f"缺：{absent}")

    # 3b) 配置页覆盖：schema 的每个顶层键必须有归属（某个分区，或显式隐藏清单）。
    # v0.47.2 实锤：v0.47.0 加了 card_scrim / card_seal_pos 却没登记分区，
    # 工坊「配置」页里**完全看不到**（schema 有、页面没有），只能去 AstrBot 后台改。
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    sec_block = re.search(r"var CONFIG_SECTIONS = \[(.*?)\n      \];", page, re.S)
    check("页面存在 CONFIG_SECTIONS", sec_block is not None)
    covered = set()
    if sec_block:
        for keys_block in re.findall(r"keys:\s*\[(.*?)\]", sec_block.group(1), re.S):
            covered.update(re.findall(r'"(\w+)"', keys_block))
    hid_block = re.search(r"var CONFIG_HIDDEN = \[(.*?)\];", page, re.S)
    hidden = set(re.findall(r'"(\w+)"', hid_block.group(1))) if hid_block else set()
    lost = [k for k in schema if k not in covered and k not in hidden]
    check("schema 配置键在配置页有归属（分区或隐藏清单）", not lost, f"缺：{lost}")
    stale = sorted(k for k in covered if k not in schema)
    check("配置页分区键都在 schema 里（防拼错）", not stale, f"多余：{stale}")

    # 4) 数据层方法
    absent_methods = [m for m in STORE_METHODS if f"def {m}(" not in store]
    check("新 store 方法全部存在", not absent_methods, f"缺：{absent_methods}")

    # 5) 老库可升级：迁移必须靠 ALTER，不能重建表
    check("迁移用 PRAGMA 探测 + ALTER（不重建表）",
          "PRAGMA table_info" in store and "ALTER TABLE" in store)

    # 6) 星语式调试交互：重置类按钮只在「查到状态」之后出现，且高级区不再常驻
    # v0.28.0：调试页分页后条件变成 `dbgTab === 'fix' && dbgState`，两种形态都算
    gate = page.count('v-if="dbgState"') + page.count("&& dbgState")
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
               "doDisablePortrait", "doDeletePortraitHard",
               "movePortraitToRef", "moveRefToPortrait"):
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
    # v0.25.3：批量补图的作品多选不能带「全部作品」哨兵（value="" 会被当成
    # 真实 series 发给后端，过滤后 0 命中）
    check("批量补图作品下拉排除空值哨兵",
          "batchSeriesOpts" in page
          and "return !!o.value" in page
          and ':options="batchSeriesOpts"' in page)
    # v0.28.0：调试页分页（按使用频率排序，回收站最后）
    check("调试页分页存在且回收站排最后",
          '{ key: "state", label: "状态与修签" }' in page
          and '{ key: "trash", label: "回收站" }' in page
          and page.index('{ key: "state"')
          < page.index('{ key: "trash"'))
    check("状态查询与修签合并在同一页（不再拆成 fix）",
          "修签 / 重渲染" not in page
          and page.count("dbgTab === 'state'") >= 5)
    check("调试页九张卡片都受分页控制", page.count("dbgTab === ") >= 8,
          str(page.count("dbgTab === ")))
    # v0.26.2：批量停用/启用的两段确认必须看得见（旧版首次点击静默 return）
    check("批量立绘确认态有可见文案与提示",
          "bulkOffText" in page and "确认停用 " in page
          and "再点一次" in page and "bulkBusy" in page)
    # v0.25.6：批量补图是后端常驻队列，关掉弹窗也要能看见进度
    check("补图进度跟到工具栏按钮（不依赖弹窗打开）",
          "batchBtnText" in page
          and "补图中 " in page
          and "if (batchOpen.value || running)" in page
          and "pollBatch()" in page)

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

    # v0.71.11：换签旧卡保留——卡面文件名带 nonce（首抽/重发重渲染两处），
    # 总览时间线与记录详情流水按事件附卡缩略；升级前共享文件名的旧路径
    # （与更新的同用户事件重复）不展示缩略，防张冠李戴
    main_src = (ROOT / "main.py").read_text(encoding="utf-8")
    check("卡面文件名带 nonce（_produce 与重发重渲染两处）",
          'f"{uid}_{date}_{int(nonce or 0)}.png"' in main_src
          and "f\"{uid}_{date}_{int(existing.get('nonce') or 0)}.png\"" in main_src
          and main_src.count('f"{uid}_{date}.png"') == 0,
          main_src.count('f"{uid}_{date}.png"'))
    check("总览/记录详情按事件附卡缩略（路径去重防覆盖张冠李戴）",
          "newer_paths" in api and api.count("card_thumb") >= 4
          and "card_thumb" in page and "openViewer(e.card_path" in page)

    if FAILURES:
        print(f"test_webui_contract: {len(FAILURES)} FAILED")
        sys.exit(1)
    print("test_webui_contract: ALL OK")


if __name__ == "__main__":
    main()
