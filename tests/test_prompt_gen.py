# -*- coding: utf-8 -*-
"""prompt_gen.py 纯函数自检（可直接运行，退出码即结论；不联网、不依赖 astrbot）。

守卫点：决定论（同种子同选择）、分布非退化（多种子下维度会变）、本地指令结构
（Qwen-Image-Edit「动作+目标+约束」）、LLM 输出清洗、t2i tag 追加、
生成主入口的兜底（无 astrbot 环境下不抛异常）。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import prompt_gen  # noqa: E402


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL: {name} {detail}")
        sys.exit(1)
    print(f"ok: {name}")


class _BrokenContext:
    """无 astrbot 环境桩：任何探测都抛错。"""

    def get_provider_by_id(self, pid):
        raise RuntimeError("no astrbot")


ENTRY = {"id": "miku", "name": "初音未来", "series": "Vocaloid",
         "tags_en": "hatsune miku, twintails", "strategy_type": "img2img",
         # v0.29.4：默认样例带 1 张显式参考图（历史用例的「单图源」口径）
         "strategy": {"ref": "refs/main.png"}}

# ---------- pick_variations：决定论 ----------
p1 = prompt_gen.pick_variations(12345)
p2 = prompt_gen.pick_variations(12345)
check("同种子同选择", p1 == p2)
check("十一个维度齐全（v0.25.7 取景，v0.32.0 鞋袜，v0.34.1 头饰/外套）",
      set(p1.keys()) == {"compose", "framing", "artstyle", "outfit", "footwear",
                         "headwear", "outerwear", "background", "pose",
                         "expression", "body"})
check("维度取自池内", p1["background"] in prompt_gen.BACKGROUNDS
      and p1["body"] in prompt_gen.BODY
      and p1["compose"] in prompt_gen.COMPOSE
      # 服装可能带「颜色前缀」（v0.13.1 独立配色维度），款式部分必须在池内
      and any(it["zh"] and it["zh"] in p1["outfit"]["zh"]
              for it in prompt_gen.OUTFITS))

# 分布非退化：1000 个种子里服装至少出现 8 种（排除全撞同一项的退化）
seen = set()
for seed in range(1000):
    seen.add(id(prompt_gen.pick_variations(seed)["outfit"]))
check("服装维度分布非退化", len(seen) >= 8)

# body 大权重保持：v0.19.10 起空项权重 90/100——默认跟随参考图/角色原本体型
keep = prompt_gen.BODY[0]
keep_n = sum(1 for s in range(2000)
             if prompt_gen.pick_variations(s)["body"].get("zh") == "")
check("体型维度默认保持原样（90% 上下）", 1650 <= keep_n <= 1950)

# ---------- 本地编辑指令 ----------
picks = prompt_gen.pick_variations(777)
inst = prompt_gen.build_local_instruction(ENTRY, picks)
check("指令含服装改动", "服装" in inst or "换成" in inst)
check("指令句尾强调保持一致", "面部特征" in inst and "辨识度" in inst)
check("指令提及竖版立绘", "竖版" in inst)
zh_seg = prompt_gen.variation_zh_list(picks)
check("变化短语列表跳过保持项", all(zh for _, zh in zh_seg))

# 全维度保持 → 通用重绘指令
empty_picks = {k: {"zh": "", "en": ""} for k in prompt_gen.DIMS}
inst0 = prompt_gen.build_local_instruction(ENTRY, empty_picks)
check("无变化时是通用重绘指令", "根据参考图" in inst0 and "面部特征" in inst0)

# ---------- t2i tag 追加（danbooru 系） ----------
tag_prompt = prompt_gen.build_t2i_prompt(ENTRY, prompt_gen.pick_variations(42))
check("t2i 保留角色 tags", "hatsune miku" in tag_prompt)
check("t2i 含构图基础词", tag_prompt.startswith("1girl, solo"))
en_extra = [en for k in prompt_gen.DIMS
            if (en := prompt_gen.pick_variations(42)[k].get("en"))]
if en_extra:
    check("t2i 追加变化 tag", all(e in tag_prompt for e in en_extra))

# ---------- t2i 自然语言系（MOSS 口径）与 Qwen 长描述兜底 ----------
nat = prompt_gen.build_t2i_natural_local(ENTRY, prompt_gen.pick_variations(42))
check("natural 兜底按 MOSS Tag 顺序（Quality 开头）", nat.startswith("best quality, highres, solo"))
check("natural 兜底保留角色 tags", "hatsune miku" in nat)
check("natural 兜底禁质量词堆砌", "8k" not in nat and "highly detailed" not in nat)
qwl = prompt_gen.build_t2i_qwen_local(ENTRY, prompt_gen.pick_variations(42))
check("qwen 兜底是中文长描述", "角色立绘插画" in qwl and "。" in qwl)

# 风格探测：无 astrbot 环境 → 回落 danbooru（安全默认）
check("探测失败回落 danbooru",
      prompt_gen.detect_t2i_style(_BrokenContext(), "moss") == "danbooru")

# 分风格元提示词
mp_nat = prompt_gen.build_t2i_meta_prompt(ENTRY, prompt_gen.pick_variations(777), "natural")
check("natural 元提示词含身份锚定要求", "Character:" in mp_nat and "Identity tags" in mp_nat)
mp_q = prompt_gen.build_t2i_meta_prompt(ENTRY, prompt_gen.pick_variations(777), "qwen")
check("qwen 元提示词要求中文长描述", "连贯中文长描述" in mp_q and "初音未来" in mp_q)

# ---------- LLM 输出清洗 ----------
check("剥 markdown 代码块", prompt_gen.sanitize_llm_output(
    "```\n把服装换成水手服\n```") == "把服装换成水手服")
check("剥引号与前缀", prompt_gen.sanitize_llm_output(
    '编辑指令：“把背景改为纯白，保持人物不变。”') == "把背景改为纯白，保持人物不变。")
check("取首个非空行", prompt_gen.sanitize_llm_output(
    "第一行指令\n第二行解释") == "第一行指令")
check("限长", len(prompt_gen.sanitize_llm_output("x" * 500)) <= 150)
check("空输入", prompt_gen.sanitize_llm_output("") == "")

# ---------- 主题预设（v0.5.9 试画台预览） ----------
check("random/空主题 = None", prompt_gen.get_theme("") is None
      and prompt_gen.get_theme("random") is None)
sw = prompt_gen.get_theme("swimsuit")
check("主题表齐全", sw is not None and len(prompt_gen.THEMES) >= 9)
# 主题关键词必须能在池里筛出子集（写错关键词 = 主题悄悄失效）
for t in prompt_gen.THEMES:
    for kw_key, dim in (("outfit_kw", "outfit"), ("bg_kw", "background"),
                        ("pose_kw", "pose"), ("compose_kw", "compose"),
                        ("expression_kw", "expression"),
                        ("artstyle_kw", "artstyle")):
        for kw in t.get(kw_key) or []:
            if not any(kw in str(it.get("zh") or "") for it in prompt_gen.DIMS[dim]):
                check(f"主题 {t['id']} 关键词命中池", False, f"{kw_key}:{kw}")
check("主题关键词全部命中池", True)

p_rand = prompt_gen.pick_variations(12345)
p_th = prompt_gen.pick_variations(12345, sw)
# v0.14.0 泳装子集含比基尼系（zh 不含「泳装」二字），判定改为子集成员匹配——
# 服装可能带颜色前缀（v0.13.1 独立配色），只要款式部分落在主题子集即可


def _in_subset(zh: str, kws: list, dim: str) -> bool:
    return any(z and z in str(zh or "")
               for z in (str(it.get("zh") or "") for it in prompt_gen.DIMS[dim])
               if any(kw in z for kw in kws))


check("主题后服装在泳装子集", _in_subset(p_th["outfit"]["zh"], sw["outfit_kw"], "outfit"))
check("主题后背景在海边子集", _in_subset(p_th["background"]["zh"], sw["bg_kw"], "background"))
check("主题不影响其它维度", p_th["pose"] == p_rand["pose"]
      and p_th["compose"] == p_rand["compose"])
check("主题决定论", prompt_gen.pick_variations(12345, sw) == p_th)
# 随机模式与主题版不同：单种子可能撞同一项，多种子扫描至少出现差异（子集过滤生效）
_diff = any(prompt_gen.pick_variations(s)["outfit"] != prompt_gen.pick_variations(s, sw)["outfit"]
            for s in range(50))
check("随机模式与主题版不同（子集过滤生效）", _diff)

# ---------- v0.14.0 主题子集多样性（用户反馈「泳装永远连体泳衣」的回归守卫） ----------
# 泳装子集 >= 8 款且比基尼至少 3 款；背景 >= 4 项
_sw_outfits = [it for it in prompt_gen.OUTFITS
               if any(kw in str(it.get("zh") or "") for kw in sw["outfit_kw"])]
_sw_bikini = [it for it in _sw_outfits if "比基尼" in str(it.get("zh") or "")]
check("泳装主题款式池 >= 8", len(_sw_outfits) >= 8)
check("泳装主题比基尼 >= 3 款", len(_sw_bikini) >= 3)
_sw_bgs = [it for it in prompt_gen.BACKGROUNDS
           if any(kw in str(it.get("zh") or "") for kw in sw["bg_kw"])]
check("泳装主题背景池 >= 4 项", len(_sw_bgs) >= 4)
# 每个内容主题的 outfit 子集 >= 5 款、background 子集 >= 4 项（模板主题不设服装约束）
for t in prompt_gen.THEMES:
    if t["id"] == "random" or str(t["id"]).startswith("tpl_"):
        continue
    n_outfit = len([it for it in prompt_gen.OUTFITS
                    if any(kw in str(it.get("zh") or "") for kw in (t.get("outfit_kw") or []))])
    n_bg = len([it for it in prompt_gen.BACKGROUNDS
                if any(kw in str(it.get("zh") or "") for kw in (t.get("bg_kw") or []))])
    if n_outfit:
        check(f"主题 {t['id']} 服装子集 >= 5 款", n_outfit >= 5)
        check(f"主题 {t['id']} 背景子集 >= 4 项", n_bg >= 4)
check("所有内容主题子集达标", True)
# 职场子集不得混入 JK 制服（v0.14.0 修「西装/马甲」关键词泄漏）
office = prompt_gen.get_theme("office")
_of_outfits = [it for it in prompt_gen.OUTFITS
               if any(kw in str(it.get("zh") or "") for kw in office["outfit_kw"])]
check("职场子集无 JK 泄漏", not any("JK" in str(it.get("zh") or "") for it in _of_outfits))
# 泳装主题换一版分布：200 种子里款式 >= 5 种（原来 3 款二选一）
_sw_zh = set()
for s in range(200):
    _o = prompt_gen.pick_variations(s, sw)["outfit"]["zh"]
    for it in _sw_outfits:
        if str(it.get("zh") or "") and str(it.get("zh")) in _o:
            _sw_zh.add(str(it.get("zh")))
            break
check("泳装主题 200 种子款式 >= 5 种", len(_sw_zh) >= 5)

# ---------- apply_dim_state：维度级重摇 / 锁定 / 删除 / 启用（v0.14.0） ----------
base = prompt_gen.pick_variations(42, sw)
# 锁定：fixed 覆盖主题子集结果，en 从池内反查
fx = prompt_gen.apply_dim_state(base, 42, {"fixed": {"outfit": "分体比基尼"}}, sw)
check("维度锁定生效", fx["outfit"]["zh"] == "分体比基尼" and fx["outfit"]["en"] == "bikini")
check("维度锁定不影响其它维度", fx["pose"] == base["pose"] and fx["background"] == base["background"])
check("维度锁定决定论", prompt_gen.apply_dim_state(
    base, 42, {"fixed": {"outfit": "分体比基尼"}}, sw) == fx)
# 锁定后 outfit 不带颜色前缀（所见即所得）
check("锁定款不叠配色", prompt_gen.apply_dim_state(
    base, 42, {"fixed": {"outfit": "旗袍"}}, None)["outfit"]["zh"] == "旗袍")
# 池外短语：en 兜底为 zh
fx2 = prompt_gen.apply_dim_state(base, 42, {"fixed": {"pose": "倒立旋转"}}, None)
check("池外锁定 en 兜底 zh", fx2["pose"]["zh"] == "倒立旋转" and fx2["pose"]["en"] == "倒立旋转")
# 单独重摇：attempts 改变该维度，其它维度不动；多次 attempt 会出不同结果
rer = prompt_gen.apply_dim_state(base, 42, {"attempts": {"pose": 1}}, sw)
check("单独重摇该维度可变", True)  # 占位，实际断言在下一行组合
_rer_diff = any(prompt_gen.apply_dim_state(base, 42, {"attempts": {"pose": n}}, sw)["pose"]
                != base["pose"] for n in range(1, 30))
check("单独重摇 30 次内出现过差异", _rer_diff)
_others_same = all(prompt_gen.apply_dim_state(base, 42, {"attempts": {"pose": n}}, sw)["background"]
                   == base["background"] for n in range(1, 30))
check("单独重摇不动其它维度", _others_same)
check("attempt=0 不干预", prompt_gen.apply_dim_state(
    base, 42, {"attempts": {"pose": 0}}, sw) == base)
# outfit 重摇时配色同种子重抽（款变色调也变，不会永远同一前缀）
_colors = set()
for n in range(1, 60):
    _o = prompt_gen.apply_dim_state(base, 42, {"attempts": {"outfit": n}}, sw)["outfit"]["zh"]
    for it in prompt_gen.OUTFIT_COLORS:
        c = str(it.get("zh") or "")
        if c and _o.startswith(c):
            _colors.add(c)
            break
check("outfit 重摇配色随之变化（出现过 >= 3 色）", len(_colors) >= 3)
# 删除维度：disabled 置空，提示词链路自动跳过
dis = prompt_gen.apply_dim_state(base, 42, {"disabled": ["pose", "expression"]}, sw)
check("删除维度置空", dis["pose"]["zh"] == "" and dis["expression"]["zh"] == "")
check("删除维度后短语列表跳过", all(t != "姿势动作" for t, _ in prompt_gen.variation_zh_list(dis)))
# 启用默认沉默维度：body/compose 从非空项里挑
en = prompt_gen.apply_dim_state(base, 42, {"enabled": ["body"]}, sw)
check("启用维度保证非空", en["body"]["zh"] != "")
# 优先级：disabled > fixed > attempts
pri = prompt_gen.apply_dim_state(
    base, 42, {"fixed": {"pose": "双手比出剪刀手"}, "attempts": {"pose": 1}}, sw)
check("fixed 优先于 attempts", pri["pose"]["zh"] == "双手比出剪刀手")
pri2 = prompt_gen.apply_dim_state(
    base, 42, {"fixed": {"pose": "双手比出剪刀手"}, "disabled": ["pose"]}, sw)
check("disabled 优先于 fixed", pri2["pose"]["zh"] == "")
# 非法输入不抛异常
check("非法 dim_state 安全", prompt_gen.apply_dim_state(
    base, 42, {"attempts": {"pose": "abc"}, "fixed": {"nope": "x"}}, sw) is not None)
# 显式启用 compose 可覆盖图生图强制单图（用户意图优先）
_comp = prompt_gen.apply_dim_state(
    prompt_gen.force_single_view(base), 42, {"enabled": ["compose"]}, None)
check("显式启用 compose 覆盖强制单图", _comp["compose"]["zh"] != "")

# ---------- v0.29.10：🎲 单独随机 = 全池重摇，不再困在主题子集 ----------
# 实测事故：主题=art_clay（artstyle_kw「黏土」只命中池里 1 条，且该条 v0.26.4
# 起权重归零），旧版 attempts 在主题子集内重摇 → 「画风永远黏土手办」
_clay_theme = prompt_gen.get_theme("art_clay")
check("art_clay 主题存在且画风子集单元素",
      _clay_theme is not None
      and len([it for it in prompt_gen.ARTSTYLES
               if any(kw in str(it.get("zh") or "")
                      for kw in (_clay_theme.get("artstyle_kw") or []))]) == 1)
_cx = prompt_gen.pick_variations(4242, _clay_theme)
check("art_clay 主题种子初摇画风=黏土（子集约束仍在）",
      "黏土" in str(_cx["artstyle"]["zh"] or ""))
_rer_clay = [prompt_gen.apply_dim_state(_cx, 4242, {"attempts": {"artstyle": n}},
                                        _clay_theme)["artstyle"]["zh"]
             for n in range(1, 41)]
check("attempts 重摇脱离子集（40 次不出现黏土）",
      all("黏土" not in z for z in _rer_clay))
check("attempts 重摇分布非退化（40 次 >= 8 种）", len(set(_rer_clay)) >= 8)
_rer_vis = prompt_gen.apply_dim_state(
    _cx, 4242, {"attempts": {"framing": 1, "compose": 1, "body": 1}}, _clay_theme)
check("重摇后已展示维度仍非空（不摇成空项消失）",
      all(str(_rer_vis[d]["zh"] or "").strip()
          for d in ("framing", "compose", "body")))

# ---------- dim_pool_options：维度下拉选项（v0.14.0） ----------
pools = prompt_gen.dim_pool_options()
check("下拉池覆盖六维", set(pools.keys()) == set(prompt_gen.DIMS.keys()))
check("服装下拉无空项且降序", "" not in pools["outfit"]
      and len(pools["outfit"]) >= 50
      and int(next(it["w"] for it in prompt_gen.OUTFITS
                   if it["zh"] == pools["outfit"][0])) >= int(next(it["w"] for it in prompt_gen.OUTFITS
                                                                  if it["zh"] == pools["outfit"][-1])))

# ---------- 立绘模板主题 + 全身姿势池（v0.7.0；v0.29.3 起模板 = 固定底稿） ----------
# 用户口径：模板是固定提示词，不参与随机、不吃维度池——同一模板换种子必须
# 逐位一致；想换装 / 换画风走预览面板显式指定（outfit_override / fixed 锁定）。
tpl3 = prompt_gen.get_theme("tpl_3view")
tpl4 = prompt_gen.get_theme("tpl_4view")
tpl_s = prompt_gen.get_theme("tpl_standing")
tpl_single_t = prompt_gen.get_theme("tpl_single")
check("四套立绘模板主题存在",
      all(t is not None for t in (tpl3, tpl4, tpl_s, tpl_single_t)))
_TPL_PLAIN = ("outfit", "footwear", "artstyle", "body", "framing")
check("模板主题置空服装/画风/体型/取景（固定底稿口径）",
      all(all(d in (t.get("plain_dims") or []) for d in _TPL_PLAIN)
          for t in (tpl3, tpl4, tpl_s, tpl_single_t)))
for seed in (1, 42, 777, 99999):
    p3 = prompt_gen.pick_variations(seed, tpl3)
    check(f"三视图模板强制三视图(seed={seed})", "三视图" in p3["compose"]["zh"])
    check(f"三视图模板强制标准站姿(seed={seed})", "标准站姿" in p3["pose"]["zh"])
    check(f"三视图模板强制微笑(seed={seed})", "微笑" in p3["expression"]["zh"])
    check(f"三视图模板强制灰底(seed={seed})", "灰" in p3["background"]["zh"])
    check(f"三视图模板不提穿搭/画风/体型/取景(seed={seed})",
          all(p3[d]["zh"] == "" for d in _TPL_PLAIN))
_base3 = prompt_gen.pick_variations(1, tpl3)
check("三视图模板跨种子完全一致（无随机）",
      all(prompt_gen.pick_variations(s, tpl3) == _base3 for s in range(200)))
check("四视图模板跨种子完全一致",
      all(prompt_gen.pick_variations(s, tpl4)
          == prompt_gen.pick_variations(1, tpl4) for s in range(200)))
check("单图模板跨种子完全一致",
      all(prompt_gen.pick_variations(s, tpl_single_t)
          == prompt_gen.pick_variations(1, tpl_single_t) for s in range(200)))
check("站姿模板跨种子完全一致",
      all(prompt_gen.pick_variations(s, tpl_s)
          == prompt_gen.pick_variations(1, tpl_s) for s in range(200)))
p4 = prompt_gen.pick_variations(42, tpl4)
check("四视图模板强制四视图", "四视图" in p4["compose"]["zh"])
ps = prompt_gen.pick_variations(42, tpl_s)
check("站姿模板强制正常站姿构图", "正常站姿" in ps["compose"]["zh"])
check("站姿模板纯白摄影棚底（不再白/粉随机）", "纯白" in ps["background"]["zh"])
check("模板显式指定穿搭仍然生效（outfit_override 不受 plain 影响）",
      prompt_gen.pick_variations(42, tpl3, "白色婚纱")["outfit"]["zh"] == "白色婚纱")

# v0.13.1：服装配色独立抽取——旗袍/婚纱不再永远黑色
colors = [c for c in prompt_gen.OUTFIT_COLORS if c.get("zh")]
check("服装配色池 >= 10 色", len(colors) >= 10)
party = prompt_gen.get_theme("party")
party_zh = {prompt_gen.pick_variations(s, party)["outfit"]["zh"] for s in range(300)}
check("旗袍主题配色/款式多样（>= 8 种）", len(party_zh) >= 8)
black_n = sum(1 for s in range(300)
              if prompt_gen.pick_variations(s, party)["outfit"]["zh"].startswith("黑色"))
check("旗袍不再总是黑色（<40%）", black_n < 120)
check("显式指定穿搭不叠颜色前缀",
      prompt_gen.pick_variations(42, None, "白色婚纱")["outfit"]["zh"] == "白色婚纱")

# v0.13.0：三视图文案（正/侧/背 + 灰底）、单图模板、JK 款式不再只有水手服
check("三视图文案含正/侧/背与灰底",
      all(k in p3["compose"]["zh"] for k in ("三视图", "正面", "侧面", "背面", "灰")))
tpl_single = prompt_gen.get_theme("tpl_single")
check("单图模板存在", tpl_single is not None)
p1s = prompt_gen.pick_variations(42, tpl_single)
check("单图模板强制单张全身构图", "单张全身立绘" in p1s["compose"]["zh"])
check("单图模板灰底", "灰" in p1s["background"]["zh"])
campus = prompt_gen.get_theme("campus")
campus_outfits = {prompt_gen.pick_variations(s, campus)["outfit"]["zh"] for s in range(400)}
check("校园主题 JK 款式足够多样（>=5）", len(campus_outfits) >= 5)
check("服装池已扩充（>=28 款）", len(prompt_gen.OUTFITS) >= 28)
# 三视图的图生图指令不能被句尾「单人竖版立绘」拉回单图
inst_3v = prompt_gen.build_local_instruction(
    ENTRY, prompt_gen.pick_variations(42, tpl3))
check("三视图指令要求同图并列而非单人竖版",
      "单人完整竖版立绘" not in inst_3v and "并列" in inst_3v)
# 指定穿搭（outfit_override，v0.7.4）
p_fix = prompt_gen.pick_variations(42, tpl3, "白色婚纱")
check("指定穿搭覆盖随机", p_fix["outfit"]["zh"] == "白色婚纱")
check("指定穿搭不动其它维度", p_fix["compose"] == prompt_gen.pick_variations(42, tpl3)["compose"])
p_fix2 = prompt_gen.pick_variations(42, None, "  机械义体装甲  ")
check("任意文本可指定", p_fix2["outfit"]["zh"] == "机械义体装甲"
      and p_fix2["outfit"]["en"] == "机械义体装甲")
check("空串不干预", prompt_gen.pick_variations(42, None, "")["outfit"]
      == prompt_gen.pick_variations(42)["outfit"])
inst_fix = prompt_gen.build_local_instruction(ENTRY, p_fix)
check("指定穿搭进编辑指令", "把角色的服装换成白色婚纱" in inst_fix)
# 全身姿势池：zh 必须写腿/脚动作（否则 img2img 编辑指令带不动下半身）
leg_kws = ("腿", "脚", "躺", "坐", "蹲", "跳", "走", "趴", "站立")
leg_n = sum(1 for p in prompt_gen.POSES if any(k in p["zh"] for k in leg_kws))
check("姿势池含足量全身姿势（腿脚参与）", leg_n >= 10)
# 随机 compose 大权重保持普通单张
plain_n = sum(1 for s in range(2000)
              if prompt_gen.pick_variations(s)["compose"].get("zh") == "")
check("compose 维度大权重保持普通单张", plain_n >= 1500)

# custom 附加
picks_c = prompt_gen.pick_variations(777)
inst_c = prompt_gen.build_local_instruction(ENTRY, picks_c, "戴上红色蝴蝶结")
check("自定义附加进指令", "戴上红色蝴蝶结" in inst_c)
meta_c = prompt_gen.build_meta_prompt(ENTRY, picks_c, "手持小猫")
check("自定义附加进元提示词", "手持小猫" in meta_c)
nat_c = prompt_gen.build_t2i_natural_local(ENTRY, picks_c, "red ribbon")
check("natural 兜底含自定义附加", "red ribbon" in nat_c)

# ---------- 元提示词 ----------
meta = prompt_gen.build_meta_prompt(ENTRY, prompt_gen.pick_variations(777))
check("元提示词含角色信息", "初音未来" in meta and "Vocaloid" in meta)
check("元提示词含变化维度", "服装：" in meta)

# ---------- 生成主入口：无 astrbot 环境兜底 ----------
async def _run():
    return await prompt_gen.build_generation_prompt(
        _BrokenContext(),
        {"prompt_llm_enabled": True, "prompt_llm_provider": "", "variations_enabled": True},
        ENTRY, 777)

prompt_text, src = asyncio.run(_run())
check("img2img 无 LLM 环境回落本地模板", src in ("local", "static") and prompt_text)
check("回落结果仍是编辑指令", "面部特征" in prompt_text)

async def _run_t2i():
    e = dict(ENTRY, strategy_type="base")
    return await prompt_gen.build_generation_prompt(
        _BrokenContext(), {"prompt_llm_enabled": True}, e, 42)

t2i_text, t2i_src = asyncio.run(_run_t2i())
check("base 策略走 tag 拼装", "hatsune miku" in t2i_text and t2i_src == "local")

async def _run_disabled():
    e = dict(ENTRY, strategy_type="img2img")
    return await prompt_gen.build_generation_prompt(
        _BrokenContext(), {"prompt_llm_enabled": False, "variations_enabled": False},
        e, 42)

off_text, off_src = asyncio.run(_run_disabled())
check("变化关闭时按 tags 原样出图", off_src == "static" and "twintails" in off_text)

# ---------- v0.13.3：img2img 单视图守卫 + 多视图源转单图条款 ----------
# 用户实测：以多视图设定稿为源重绘立绘，输出仍是多视图。三个根因一起修：
# a) 编辑指令/元提示写明「多视图源只保留一个视角」；
# b) 非模板主题下 compose 不再随机混入三/四视图（种子决定论会复现）；
# c) tpl_ 主题（compose_kw 显式约束）仍可出多视图设定稿。
check("本地编辑指令（非多视图）带转单图条款", "只保留其中一个视角" in inst)
check("无变化兜底指令也带转单图条款", "只保留其中一个视角" in inst0)
inst_3v2 = prompt_gen.build_local_instruction(ENTRY, prompt_gen.pick_variations(42, tpl3))
check("三视图模板指令仍要求并列多视角",
      "并列" in inst_3v2 and "三视图" in inst_3v2
      and "只保留其中一个视角" not in inst_3v2)
p_multi = prompt_gen.pick_variations(42, tpl3)
fsv = prompt_gen.force_single_view(dict(p_multi))
check("force_single_view 置空 compose（纯函数不动原 dict）",
      fsv["compose"]["zh"] == "" and fsv["compose"]["en"] == ""
      and p_multi["compose"]["zh"] != "")
check("元提示词不再硬编码「源是单人立绘」",
      "官方立绘（单人）" not in meta
      and "多视角拼接" in meta and "收敛为单张立绘" in meta)

async def _run_i2i(seed):
    return await prompt_gen.build_generation_prompt(
        _BrokenContext(),
        {"prompt_llm_enabled": True, "prompt_llm_provider": "",
         "variations_enabled": True},
        ENTRY, seed)

bad_seeds = []
for s in range(60):
    txt, _s = asyncio.run(_run_i2i(s))
    if ("三视图" in txt) or ("四视图" in txt) or ("multiple views" in txt.lower()):
        bad_seeds.append(s)
check("img2img 非模板主题 60 种子无随机多视图（命中种子 " + str(bad_seeds) + "）",
      not bad_seeds)

async def _run_i2i_tpl(seed):
    return await prompt_gen.build_generation_prompt(
        _BrokenContext(),
        {"prompt_llm_enabled": True, "prompt_llm_provider": "",
         "variations_enabled": True},
        ENTRY, seed, theme_id="tpl_3view")

txt_tpl, _src_tpl = asyncio.run(_run_i2i_tpl(7))
check("tpl_3view 主题仍输出三视图指令", "三视图" in txt_tpl)

# ---------- v0.31.1：模板主题跳过 LLM 通道（固定底稿逐位确定） ----------
# 用户实测：三视图模板「有时候」得到 LLM 改写的压缩指令（丢并列设定稿强调 /
# 参考图对齐条款，文生图 qwen 通道甚至写出「把构图改为…」的编辑指令口吻）。
# v0.29.3 已定口径「模板是固定提示词，不能充满随机性」——LLM 改写就是随机源，
# tpl_ 主题必须短路 LLM、逐位确定。
class _LlmContext:
    """带可用对话模型的桩：text_chat 记录调用，验证 LLM 通道是否真被短路。"""

    def __init__(self):
        self.calls = []

    def get_provider_by_id(self, pid):
        raise RuntimeError("no astrbot")

    async def get_using_provider_async(self):
        return self

    def get_all_providers(self):
        return [self]

    async def text_chat(self, prompt, session_id=None, system_prompt=""):
        self.calls.append({"prompt": prompt, "system": system_prompt})

        class _R:
            completion_text = "把构图改为正面侧面背面同框的三视图，LLM改写版"

        return _R()


async def _run_llm(ctx, e, theme_id=""):
    return await prompt_gen.build_generation_prompt(
        ctx,
        {"prompt_llm_enabled": True, "prompt_llm_provider": "",
         "variations_enabled": True},
        e, 7, theme_id=theme_id)


_llm = _LlmContext()
t1, s1 = asyncio.run(_run_llm(_llm, ENTRY, "tpl_3view"))
t2, s2 = asyncio.run(_run_llm(_llm, ENTRY, "tpl_3view"))
check("模板 + LLM 开启也走本地指令（不调用对话模型）",
      s1 == "local" and len(_llm.calls) == 0)
check("模板指令逐位确定（两次调用完全一致）", t1 == t2)
check("模板指令带并列设定稿与参考图对齐条款",
      "并列展示上述各个视角" in t1 and "输出角色设定稿" in t1
      and "确保各视角是同一个角色、同一套服装" in t1)

# ---------- v0.32.0：鞋袜维度——跟随穿搭家族（legwear + footwear） ----------
# 用户实测：全身照的鞋/裤袜永远跟参考图（泳装配乐福鞋、婚纱配运动鞋）——
# 维度池只有服装、从不提鞋袜。新增 footwear 维度：按「穿搭家族」派生，
# 该光腿光腿、该丝袜丝袜；模板底稿与空穿搭不提及（跟随角色原本）。
check("鞋袜进维度表与展示序",
      "footwear" in prompt_gen.DIMS
      and any(t == "footwear" for _l, t in prompt_gen.DIM_ORDER))
check("穿搭家族全覆盖（74 条穿搭无一漏到泛用兜底）",
      all(prompt_gen.feet_candidates(str(it.get("zh") or ""))
          is not prompt_gen._FEET_GENERIC for it in prompt_gen.OUTFITS))
_feet_inv_bad = []
for tid in prompt_gen.content_theme_ids() + ["tpl_3view", "tpl_single",
                                             "tpl_standing"]:
    _ft = prompt_gen.get_theme(tid)
    for _fs in range(60):
        _fp = prompt_gen.pick_variations(_fs, _ft)
        _oz = str(_fp["outfit"]["zh"] or "").strip()
        _fz = str(_fp["footwear"]["zh"] or "").strip()
        _cand = {it["zh"] for it in prompt_gen.feet_candidates(_oz)}
        if (_oz and _fz not in _cand) or (not _oz and _fz):
            _feet_inv_bad.append((tid, _fs, _oz, _fz))
check("鞋袜跟随实际穿搭的家族（全部主题+模板各 60 种子零违例）",
      not _feet_inv_bad, str(_feet_inv_bad[:3]))
_feet_var = {prompt_gen.pick_variations(s)["footwear"]["zh"] for s in range(400)}
check("鞋袜分布非退化（400 种子 >= 12 种：光腿/丝袜/裤袜/各式鞋）",
      len(_feet_var) >= 12)
check("泳装主题 60 种子必出光脚/凉鞋/光腿（不再配乐福鞋）",
      all(prompt_gen.pick_variations(s, prompt_gen.get_theme("swimsuit"))
          ["footwear"]["zh"] in {"光脚", "凉鞋", "光腿"} for s in range(60)))
check("模板底稿鞋袜置空（跟随角色原本）",
      prompt_gen.pick_variations(42, tpl3)["footwear"]["zh"] == "")
check("鞋袜可单独重摇且非空",
      prompt_gen.apply_dim_state(prompt_gen.pick_variations(42), 42,
                                 {"attempts": {"footwear": 1}},
                                 None)["footwear"]["zh"].strip() != "")
check("鞋袜可锁定（en 池内反查）",
      prompt_gen.apply_dim_state(prompt_gen.pick_variations(42), 42,
                                 {"fixed": {"footwear": "黑色裤袜"}},
                                 None)["footwear"]["en"] == "black pantyhose")
_ft_campus = prompt_gen.get_theme("campus")
_ft_rer = prompt_gen.apply_dim_state(
    prompt_gen.pick_variations(42, _ft_campus), 42,
    {"attempts": {"outfit": 1}}, _ft_campus)
_ft_oz = str(_ft_rer["outfit"]["zh"] or "")
check("重摇穿搭后鞋袜跟随新穿搭的家族",
      _ft_rer["footwear"]["zh"]
      in {it["zh"] for it in prompt_gen.feet_candidates(_ft_oz)})
_ft_fx = prompt_gen.apply_dim_state(
    prompt_gen.pick_variations(42), 42, {"fixed": {"outfit": "旗袍"}}, None)
check("锁定旗袍 → 鞋袜落在高跟鞋族",
      _ft_fx["footwear"]["zh"]
      in {it["zh"] for it in prompt_gen._F_HEELS})
_ft_dis = prompt_gen.apply_dim_state(
    prompt_gen.pick_variations(42, _ft_campus), 42,
    {"disabled": ["outfit"]}, _ft_campus)
check("移除穿搭 → 鞋袜一并置空（不与未提及的穿搭脱节）",
      _ft_dis["footwear"]["zh"] == "")
_ft_pf = prompt_gen.pick_variations(42, prompt_gen.get_theme("campus"))
check("编辑指令含鞋袜条款",
      "把鞋袜改为" in prompt_gen.build_local_instruction(
          ENTRY, _ft_pf, ref_mode="none"))
check("danbooru tags 含鞋袜 en",
      any(x in prompt_gen.build_t2i_prompt(ENTRY, _ft_pf)
          for x in ("kneehighs", "loafers", "socks", "thighhighs",
                    "bare legs")))
check("qwen 中文描述含鞋袜",
      any(x in prompt_gen.build_t2i_qwen_local(ENTRY, _ft_pf)
          for x in ("及膝", "乐福", "短袜", "过膝", "光腿")))
check("自填穿搭命中家族（女仆装 → 玛丽珍）",
      "玛丽珍" in prompt_gen.pick_variations(
          42, None, outfit_override="女仆装")["footwear"]["zh"])

# ---------- v0.34.1：头饰 / 外套两维——分离 + 自洽沉默 + 权重按真实搭配 ----------
# v0.33.0 的合并「帽子外套」太敷衍：①摘掉 95% 一刀切（冬装配大衣时应穿外套、
# 女巫尖顶帽是穿搭的一部分）；②帽子与外套绑死成一条。拆成 headwear/outerwear
# 两维独立派生，穿搭本身是帽/外套的（女巫斗篷与尖顶帽、冬季呢子校服大衣）
# 对应维度沉默置空，交给穿搭条目自定义。
_OE = {"id": "x", "name": "测试娘",
       "tags_en": "1girl, baseball cap, black jacket, blue hair, fox ears"}
check("头饰/外套进维度表与展示序",
      "headwear" in prompt_gen.DIMS and "outerwear" in prompt_gen.DIMS
      and any(t == "headwear" for _l, t in prompt_gen.DIM_ORDER)
      and any(t == "outerwear" for _l, t in prompt_gen.DIM_ORDER))
check("头饰/外套家族全覆盖（关键词命中，无泛用兜底漏网）",
      all(any(any(k in str(it.get("zh") or "") for k in keys)
              for keys, _c in prompt_gen._HEADWEAR_FAMILIES)
          for it in prompt_gen.OUTFITS)
      and all(any(any(k in str(it.get("zh") or "") for k in keys)
              for keys, _c in prompt_gen._OUTERWEAR_FAMILIES)
          for it in prompt_gen.OUTFITS))
_oe_inv_bad = []
for tid in prompt_gen.content_theme_ids() + ["tpl_3view", "tpl_single"]:
    _ot = prompt_gen.get_theme(tid)
    for _os in range(60):
        _op = prompt_gen.pick_variations(_os, _ot)
        _oz = str(_op["outfit"]["zh"] or "").strip()
        for _tag, _cand_fn, _silent in prompt_gen._FOLLOW_DIMS:
            if _tag == "footwear":
                continue
            _vz = str(_op[_tag]["zh"] or "").strip()
            _cand = {it["zh"] for it in _cand_fn(_oz)}
            if _oz and _vz and _vz not in _cand:
                _oe_inv_bad.append((tid, _os, _tag, _oz, _vz))
check("头饰/外套跟随实际穿搭的家族（主题+模板各 60 种子零违例）",
      not _oe_inv_bad, str(_oe_inv_bad[:3]))
_op_witch = prompt_gen.pick_variations(42)
_op_witch["outfit"] = {"w": 0, "zh": "女巫斗篷与尖顶帽", "en": "witch hat, cape"}
_op_w2 = prompt_gen.pick_variations(42, None, outfit_override="女巫斗篷与尖顶帽")
check("自洽：女巫斗篷与尖顶帽 → 头饰与外套都沉默（交给穿搭定义）",
      _op_w2["headwear"]["zh"] == "" and _op_w2["outerwear"]["zh"] == "")
_op_w3 = prompt_gen.pick_variations(42, None, outfit_override="冬季呢子校服大衣与围巾")
check("自洽：大衣穿搭 → 外套沉默但头饰仍活跃（毛线帽/摘掉）",
      _op_w3["outerwear"]["zh"] == ""
      and _op_w3["headwear"]["zh"] in {"摘掉帽子", "戴上毛线帽", "戴上贝雷帽"})
# 锁定为水手服JK制服 → 头饰跟随 JK 家族（贝雷帽/发带/摘掉，戴帽 20%~50%）
_jk_fixed = prompt_gen.apply_dim_state(
    prompt_gen.pick_variations(42), 42,
    {"fixed": {"outfit": "水手服JK制服"}}, None)
check("锁定水手服JK → 头饰落在 JK 家族",
      _jk_fixed["headwear"]["zh"] in {it["zh"] for it in prompt_gen._FHW_JK},
      _jk_fixed["headwear"]["zh"])
_jk_hat = sum(1 for s in range(300)
              if prompt_gen.apply_dim_state(
                  prompt_gen.pick_variations(s), s,
                  {"fixed": {"outfit": "水手服JK制服"}},
                  None)["headwear"]["zh"] != "摘掉帽子")
check("头饰不一成不变（JK 家族戴帽占比 20%~50%）",
      0.20 <= _jk_hat / 300 <= 0.50, f"{_jk_hat}/300")
_sw_ow = sum(1 for s in range(300)
             if prompt_gen.pick_variations(s, prompt_gen.get_theme("swimsuit"))
             ["outerwear"]["zh"] == "换上防晒开衫")
check("泳装外套偶尔换防晒开衫（5%~30%）", 0.05 <= _sw_ow / 300 <= 0.30,
      f"{_sw_ow}/300")
_sw_ow = sum(1 for s in range(300)
             if prompt_gen.pick_variations(s, prompt_gen.get_theme("swimsuit"))
             ["outerwear"]["zh"] == "换上防晒开衫")
check("泳装外套偶尔换防晒开衫（5%~30%）", 0.05 <= _sw_ow / 300 <= 0.30,
      f"{_sw_ow}/300")
check("模板底稿头饰/外套置空（保留角色原本）",
      prompt_gen.pick_variations(42, tpl3)["headwear"]["zh"] == ""
      and prompt_gen.pick_variations(42, tpl3)["outerwear"]["zh"] == "")
_op_sw = prompt_gen.pick_variations(11, prompt_gen.get_theme("swimsuit"))
check("编辑指令含独立头饰/外套子句",
      any(x in prompt_gen.build_local_instruction(
          _OE, _op_sw, ref_mode="none")
          for x in ("摘掉帽子", "戴上草帽", "戴上宽檐遮阳帽")))
_stripped = prompt_gen.build_t2i_prompt(_OE, _op_sw)
check("danbooru tags 已剥离 hat 与 jacket（头饰/外套分词干，保留其余）",
      "cap" not in _stripped and "jacket" not in _stripped
      and "blue hair" in _stripped and "fox ears" in _stripped)
check("qwen 中文描述已剥离 hat/jacket",
      "cap" not in prompt_gen.build_t2i_qwen_local(_OE, _op_sw).lower()
      and "jacket" not in prompt_gen.build_t2i_qwen_local(_OE, _op_sw))
check("元提示词 tags 同步剥离（不与维度指令矛盾）",
      "cap" not in prompt_gen.build_t2i_meta_prompt(_OE, _op_sw, "qwen")
      and "jacket" not in prompt_gen.build_t2i_meta_prompt(_OE, _op_sw, "qwen"))
_op_keep = prompt_gen.pick_variations(11)
_op_keep["outfit"] = {"w": 0, "zh": "", "en": ""}
_op_keep["headwear"] = {"w": 0, "zh": "", "en": ""}
_op_keep["outerwear"] = {"w": 0, "zh": "", "en": ""}
check("空穿搭（跟随角色原本）保留 hat/jacket tags",
      "baseball cap" in prompt_gen._entry_tags(_OE, _op_keep)
      and "black jacket" in prompt_gen._entry_tags(_OE, _op_keep))
check("词干剥离互不越界（头饰剥帽不碰 jacket、外套剥衣不碰 cap）",
      "black jacket" in prompt_gen.strip_tags_by_stems(
          "black jacket, red beret", prompt_gen._HEAD_STRIP_STEMS)
      and "red beret" in prompt_gen.strip_tags_by_stems(
          "black jacket, red beret", prompt_gen._COAT_STRIP_STEMS))
_or_rer = prompt_gen.apply_dim_state(
    prompt_gen.pick_variations(42), 42, {"attempts": {"headwear": 1}}, None)
check("头饰可单独重摇且非空", _or_rer["headwear"]["zh"].strip() != "")
_or_fx = prompt_gen.apply_dim_state(
    prompt_gen.pick_variations(42), 42, {"fixed": {"outfit": "旗袍"}}, None)
check("锁定旗袍 → 头饰/外套都落在礼服族（摘掉）",
      _or_fx["headwear"]["zh"]
      in {it["zh"] for it in prompt_gen._FHW_PLAIN}
      and _or_fx["outerwear"]["zh"]
      in {it["zh"] for it in prompt_gen._FOW_PLAIN})


_orig_detect = prompt_gen.detect_t2i_style
prompt_gen.detect_t2i_style = lambda ctx, wf: "qwen"
try:
    _llm2 = _LlmContext()
    qb, sb = asyncio.run(_run_llm(
        _llm2, dict(ENTRY, strategy_type="base"), "tpl_3view"))
    check("模板 + qwen 文生图走本地长描述（不调用对话模型）",
          sb == "local" and len(_llm2.calls) == 0
          and "并列展示正面、侧面、背面" in qb)
    _llm3 = _LlmContext()
    _nb, nb_src = asyncio.run(_run_llm(
        _llm3, dict(ENTRY, strategy_type="base"), ""))
    check("非模板主题仍走 LLM 通道（对照）",
          nb_src == "llm" and len(_llm3.calls) == 1)
finally:
    prompt_gen.detect_t2i_style = _orig_detect


# ---------- v0.22.4：文生图链路也不许随机多视图 ----------
# 现象：批量补图里 base 直出的角色吐出三/四视图拼接设定稿。根因是 COMPOSE
# 池 3/30 权重随机命中多视图，而 force_single_view 只加在 img2img 分支，
# 文生图（base/lora）连提示词带 LLM 元提示都写着「角色设定稿」。
check("三/四视图项权重归零（退出随机池）",
      all(it["w"] == 0 for it in prompt_gen.COMPOSE
          if "三视图" in it["zh"] or "四视图" in it["zh"]))
check("模板主题仍能精确命中三视图",
      "三视图" in prompt_gen.pick_variations(42, tpl3)["compose"]["zh"])

async def _run_t2i_multi(seed):
    e = dict(ENTRY, strategy_type="base")
    return await prompt_gen.build_generation_prompt(
        _BrokenContext(),
        {"prompt_llm_enabled": True, "prompt_llm_provider": "",
         "variations_enabled": True}, e, seed)

bad_t2i = []
for s in range(60):
    txt, _s = asyncio.run(_run_t2i_multi(s))
    low = txt.lower()
    if ("三视图" in txt or "四视图" in txt or "multiple views" in low
            or "character sheet" in low):
        bad_t2i.append(s)
check("base 文生图 60 种子无随机多视图（命中 " + str(bad_t2i) + "）",
      not bad_t2i)
check("随档维度不依赖调用方 force_single",
      prompt_gen.describe_variations(42)
      == prompt_gen.describe_variations(42, force_single=True))
check("tpl_3view 随档仍记录多视图构图",
      any("三视图" in zh for _l, zh in
          prompt_gen.describe_variations(42, "tpl_3view")))

# ---------- v0.19.0：画风维度（用户反馈「主题里应该加上画风，现在画风太单一」） ----------
check("画风池 >= 30 款风格",
      len([s for s in prompt_gen.ARTSTYLES if s["zh"]]) >= 30)
# v0.19.1：介质系风格（不是「换上色方式」而是「换制作工艺」）必须在池
for _kw in ("蚀刻", "彩绘玻璃", "Riso", "Y2K", "岩彩", "钢笔淡彩", "电影感",
            "复古印刷", "故障", "美式漫画", "韩漫", "低多边形", "剪纸", "细密画",
            "丝网印刷"):
    check(f"介质画风在池（{_kw}）",
          any(_kw in str(s.get("zh") or "") for s in prompt_gen.ARTSTYLES))
_az_zh = {prompt_gen.pick_variations(s)["artstyle"]["zh"] for s in range(400)}
check("画风维度分布非退化（400 种子 >= 8 种）", len(_az_zh) >= 8)
_az_keep = sum(1 for s in range(2000)
               if prompt_gen.pick_variations(s)["artstyle"].get("zh") == "")
check("画风保留少量空项（0 < 不写画风种数 < 25%）", 0 < _az_keep < 500)
# 画风必须四路都进：danbooru tag / natural caption / qwen 中文 / img2img 编辑指令
_p_az = prompt_gen.apply_dim_state(
    base, 42, {"fixed": {"artstyle": "清透水彩绘本"}}, None)
check("画风可锁定（en 从池内反查）",
      _p_az["artstyle"]["zh"] == "清透水彩绘本"
      and "watercolor" in _p_az["artstyle"]["en"])
check("画风 tag 进 danbooru 串",
      "watercolor" in prompt_gen.build_t2i_prompt(ENTRY, _p_az))
check("画风 tag 进 natural 兜底",
      "watercolor" in prompt_gen.build_t2i_natural_local(ENTRY, _p_az))
check("画风进 qwen 中文兜底",
      "一幅清透水彩绘本的单人角色立绘插画"
      in prompt_gen.build_t2i_qwen_local(ENTRY, _p_az))
_az_inst = prompt_gen.build_local_instruction(ENTRY, _p_az)
check("画风进图生图编辑指令并再锚一次辨识度",
      "把整体画风改为清透水彩绘本" in _az_inst and _az_inst.count("可辨认") >= 1)
check("画风进变化短语列表", ("画风", "清透水彩绘本") in prompt_gen.variation_zh_list(_p_az))
check("画风进元提示词", "画风：清透水彩绘本" in prompt_gen.build_meta_prompt(ENTRY, _p_az))
# 纯画风主题：只约束画风，服装/背景/姿势与随机完全一致
_art_ink = prompt_gen.get_theme("art_ink")
check("纯画风主题存在", _art_ink is not None and _art_ink.get("artstyle_kw") == ["水墨"])
_art_only = all(
    "水墨" in prompt_gen.pick_variations(s, _art_ink)["artstyle"]["zh"]
    and prompt_gen.pick_variations(s, _art_ink)["outfit"]
    == prompt_gen.pick_variations(s)["outfit"]
    and prompt_gen.pick_variations(s, _art_ink)["background"]
    == prompt_gen.pick_variations(s)["background"]
    for s in range(60))
check("纯画风主题只收束画风（服装/背景仍随种子）", _art_only)
# 内容主题都带画风约束（用户要求「主题里要加上画风」；tpl_ 模板 v0.29.3 起
# 固定底稿不写画风，交底模默认——所以也排除）
_no_style = [t["id"] for t in prompt_gen.THEMES
             if t["id"] != "random" and not str(t["id"]).startswith("tpl_")
             and not (t.get("artstyle_kw") or [])]
check("内容主题（除 random/tpl_）都约束画风", not _no_style)
# 主题画风子集必须真的能摇出东西（写错关键词 = 画风悄悄失效）
_bad_style_subset = []
for t in prompt_gen.THEMES:
    kws = t.get("artstyle_kw") or []
    if not kws:
        continue
    if not [it for it in prompt_gen.ARTSTYLES
            if any(kw in str(it.get("zh") or "") for kw in kws)]:
        _bad_style_subset.append(t["id"])
check("主题画风关键词全部筛出非空子集", not _bad_style_subset)
# 同一主题下画风也会变（不是锁定单一画风）
_campus_az = {prompt_gen.pick_variations(s, prompt_gen.get_theme("campus"))["artstyle"]["zh"]
              for s in range(120)}
check("校园主题画风 >= 2 种（主题内也有画风变化）", len(_campus_az) >= 2)

# ---------- v0.19.10：防双头/双人 + 体型池重做 ----------
# 背景：用户实测「趴在桌上打瞌睡」+ 全身立绘 → 桌上一颗睡着的头 + 原姿势的
# 完整人又画了一遍。三个推手一起修：睡姿 en 带自相矛盾的 full body / 指令缺
# 「单人单头」硬约束 / 体型「高挑成熟」触发过频。
# a) 睡姿类 en 不再带 full body（趴桌不可能同时呈现全身站立像）
_sleep_bad = [p["zh"] for p in prompt_gen.POSES
              if "瞌睡" in str(p.get("zh") or "") and "full body" in str(p.get("en") or "")]
check("睡姿类姿势不再带 full body tag", not _sleep_bad)
# b) 体型池补齐少女/萝莉两档，且变化合计权重 <= 15%（90+ 变化）
_bz = [str(it.get("zh") or "") for it in prompt_gen.BODY]
check("体型池含少女体型", any("少女" in z for z in _bz))
check("体型池含萝莉体型", any("萝莉" in z for z in _bz))
_body_total = sum(int(it.get("w", 1)) for it in prompt_gen.BODY)
_body_change = sum(int(it.get("w", 1)) for it in prompt_gen.BODY if it["zh"])
check("体型变化合计权重 <= 15%", _body_change * 100 <= _body_total * 15)
check("高挑成熟不再是变化里的最大头",
      int(next(it["w"] for it in prompt_gen.BODY if "高挑" in it["zh"]))
      <= _body_change // 3)
# c) 姿势类改动的本地指令带「唯一姿势」条款；所有单图指令带「只出现这一个人物」
_pose_picks = dict(prompt_gen.pick_variations(42),
                   pose={"zh": "趴在桌上打瞌睡", "en": "sleeping on desk, dozing off"})
_pose_inst = prompt_gen.build_local_instruction(ENTRY, _pose_picks)
check("姿势改动指令写明唯一姿势", "为唯一姿势" in _pose_inst and "趴在桌上打瞌睡" in _pose_inst)
check("单图指令带单人单头约束", "只出现这一个人物" in _pose_inst
      and "只保留一颗头部" in _pose_inst)
check("无变化兜底指令也带单人约束", "只出现这一个人物" in inst0)
# 多视图模板不受单人条款拉扯（设定稿并列多视角是预期行为）
_3v_inst = prompt_gen.build_local_instruction(ENTRY, prompt_gen.pick_variations(42, tpl3))
check("三视图指令不混入唯一姿势条款", "为唯一姿势" not in _3v_inst)
# d) LLM 通道：系统提示与元提示词都带单人单头纪律
check("LLM 编辑指令系统提示带画面纪律",
      "一颗头部" in prompt_gen._SYSTEM_PROMPT and "唯一姿势" in prompt_gen._SYSTEM_PROMPT)
_p_mp = prompt_gen.build_meta_prompt(ENTRY, _pose_picks)
check("元提示词带单人单头目标", "一个人物" in _p_mp and "唯一姿势" in _p_mp)
# e) 文生图两路系统提示同样强调单人
check("qwen 系统提示带单人约束",
      "一个人物" in prompt_gen._SYSTEM_T2I_QWEN)
check("natural 系统提示带 solo 约束",
      "solo" in prompt_gen._SYSTEM_T2I_NATURAL
      and "single character" in prompt_gen._SYSTEM_T2I_NATURAL)

# ---------- v0.20.0：random 主题实摇 + describe_variations 复算 ----------
_t1, _t1id = prompt_gen.resolve_theme("random", 42)
_t2, _t2id = prompt_gen.resolve_theme("random", 42)
check("random 主题实摇决定论", _t1id == _t2id and _t1 is not None)
check("random 实摇结果在内容主题池", _t1id in prompt_gen.content_theme_ids())
check("random 实摇分布非退化（200 种子 >= 8 种）",
      len({prompt_gen.resolve_theme("random", s)[1] for s in range(200)}) >= 8)
check("具体主题 id 原样透传", prompt_gen.resolve_theme("campus", 42)[1] == "campus")
check("空串仍不套主题", prompt_gen.resolve_theme("", 42) == (None, ""))
check("tpl_ 模板可透传（batch_fill 不受影响）",
      prompt_gen.resolve_theme("tpl_3view", 42)[1] == "tpl_3view")
# v0.29.10：random 摇主题尊重 art_theme_weight=0 的退池决定（art_clay）——
# v0.26.4 只清在批量轮换里，面板「随机」此前仍能摇中黏土手办主题
check("random 实摇 300 种子不含 art_clay（退池画风不走后门）",
      all(prompt_gen.resolve_theme("random", s)[1] != "art_clay"
          for s in range(300)))
_dv = prompt_gen.describe_variations(777)
check("describe_variations 与 variation_zh_list 同源",
      _dv == prompt_gen.variation_zh_list(prompt_gen.pick_variations(777)))
_dv_i2i = prompt_gen.describe_variations(777, force_single=True)
check("describe_variations force_single 不含三/四视图",
      all("视图" not in zh for _, zh in _dv_i2i))

# ---------- v0.25.7：画质三连（尾巴丢失 / 画风复古 / 全是全身） ----------
# ① 取景：非全身项占多数，全身不再是默认
_fr = prompt_gen.FRAMING
_non_full = sum(it["w"] for it in _fr if it["zh"] and "全身" not in it["zh"])
_total = sum(it["w"] for it in _fr)
check("取景维度存在且偏非全身",
      "framing" in prompt_gen.DIMS and _non_full * 100 // _total >= 60,
      f"{_non_full}/{_total}")

# ② 固有特征：尾巴/兽耳这类部件必须在指令里逐项点名
_tail_entry = {"id": "x", "name": "狐娘", "tags_en": "1girl, fox ears, tail, long hair"}
check("固有特征识别尾巴与兽耳",
      "尾巴" in prompt_gen.identity_feature_text(_tail_entry)
      and "狐狸耳" in prompt_gen.identity_feature_text(_tail_entry))
check("无固有特征的角色返回空串",
      prompt_gen.identity_feature_text({"tags_en": "1girl"}) == "")
_inst_feat = prompt_gen.build_local_instruction(
    _tail_entry, prompt_gen.pick_variations(42))
check("编辑指令逐项锚定固有特征",
      "尾巴" in _inst_feat and "不得丢失" in _inst_feat, _inst_feat[:120])
check("英文特征串给 caption 用",
      "tail" in prompt_gen.identity_feature_text(_tail_entry, "en"))

# v0.29.5：词边界匹配——twintails / ponytail 不得再误报成「尾巴」
check("twintails 不误报尾巴",
      prompt_gen.identity_features({"tags_en": "hatsune miku, twintails"}) == [])
check("ponytail / long hair 不误报",
      prompt_gen.identity_features({"tags_en": "1girl, ponytail, long hair"}) == [])
check("复数形态 fox tails 仍命中尾巴",
      ("tail", "尾巴") in prompt_gen.identity_features({"tags_en": "1girl, fox tails"}))
check("antennae 复数命中触角",
      ("antenna", "触角") in prompt_gen.identity_features({"tags_en": "1girl, antennae"}))
check("真尾巴角色照常命中",
      "尾巴" in prompt_gen.identity_feature_text(_tail_entry))

# ③ 画风现代化：现代主题权重高于复古，复古 90s 已降权
check("现代画风主题权重高于复古",
      prompt_gen.art_theme_weight("art_modern")
      > prompt_gen.art_theme_weight("art_retro")
      and prompt_gen.art_theme_weight("art_retro") == 1)
check("画风池含 2026 现代动漫画风",
      any("现代日式动画" in str(it.get("zh") or "")
          for it in prompt_gen.ARTSTYLES))
check("复古 90s 已降权",
      next(it["w"] for it in prompt_gen.ARTSTYLES if "复古 90" in str(it["zh"])) <= 2)

# ④ Qwen 文生图兜底按官方公式：首句定框架、光照单独成句、不写指令腔
_q = prompt_gen.build_t2i_qwen_local(
    {"name": "初音未来", "tags_en": "hatsune miku, twintails"},
    prompt_gen.pick_variations(42))
check("Qwen 兜底首句定框架", _q.startswith("一幅"), _q[:40])
check("Qwen 兜底光照单独成句", "光照" in _q)
check("Qwen 兜底不写指令腔", "请画" not in _q and "帮我" not in _q)

# ---------- v0.26.3：表情与主题 / 姿势协调 ----------
# ① 内容主题全部配了 expression_kw（旧版十个内容主题一个都没配，表情全池随机）
_content_themes = [t for t in prompt_gen.THEMES
                   if not str(t["id"]).startswith(("art_", "tpl_"))
                   and t["id"] != "random"]
_missing_expr = [t["id"] for t in _content_themes if not t.get("expression_kw")]
check("内容主题都配了表情子集", not _missing_expr, str(_missing_expr))

# ② 强情绪降权、柔和表情为主
_total_w = sum(int(it["w"]) for it in prompt_gen.EXPRESSIONS)
_surprised_w = sum(int(it["w"]) for it in prompt_gen.EXPRESSIONS
                   if "惊讶" in str(it["zh"]))
_soft_kw = ("微笑", "平静", "若有所思", "莞尔", "浅笑")
_soft_w = sum(int(it["w"]) for it in prompt_gen.EXPRESSIONS
              if any(k in str(it["zh"]) for k in _soft_kw))
check("惊讶类表情占比 < 5%", _surprised_w * 100 // _total_w < 5,
      f"{_surprised_w}/{_total_w}")
check("柔和类表情占比 >= 40%", _soft_w * 100 // _total_w >= 40,
      f"{_soft_w}/{_total_w}")

# ③ 主题子集生效：泳装主题下表情都落在子集里
_sw = prompt_gen.get_theme("swimsuit")
_sw_expr = {prompt_gen.pick_variations(s, _sw)["expression"]["zh"]
            for s in range(200)}
_sw_allowed = {it["zh"] for it in prompt_gen.EXPRESSIONS
               if any(kw in str(it["zh"]) for kw in _sw["expression_kw"])}
check("泳装主题表情限定在子集内",
      bool(_sw_expr) and _sw_expr <= _sw_allowed, str(_sw_expr))

# ④ 睡姿锁表情：睡着 / 躺着不许配「惊讶瞪眼」
_sleep_hit = 0
for s in range(160):
    p = prompt_gen.pick_variations(s)
    if any(h in str(p["pose"].get("zh") or "")
           for h in ("打瞌睡", "蜷缩", "睡着", "侧躺", "仰面躺")):
        _sleep_hit += 1
        if not any(k in str(p["expression"].get("zh") or "")
                   for k in ("困倦", "闭眼", "平静")):
            check("睡姿表情被锁到困倦/闭眼/平静", False,
                  str(p["expression"].get("zh")))
check("睡姿样本存在且全部锁定", _sleep_hit > 0, str(_sleep_hit))

# ---------- v0.26.4：黏土手办退出随机池（用户反馈丑） ----------
_clay = next(it for it in prompt_gen.ARTSTYLES if "黏土" in str(it["zh"]))
check("黏土手办权重归零", int(_clay["w"]) == 0, str(_clay["w"]))
check("黏土主题不进批量轮换", prompt_gen.art_theme_weight("art_clay") == 0)
check("随机抽样 1000 次不命中黏土",
      all("黏土" not in str(prompt_gen.pick_variations(s)["artstyle"].get("zh") or "")
          for s in range(1000)))
check("黏土仍保留在池里供手动锁定",
      any("黏土" in str(it["zh"]) for it in prompt_gen.ARTSTYLES))

# ---------- v0.29.4：指令按参考图形态分流（无图 / 单图 / 多图） ----------
_e_none = dict(ENTRY, strategy={})
_e_multi = dict(ENTRY, strategy={"gen_refs": ["refs/front.png", "refs/side.png",
                                              "refs/back.png"]})
check("参考图形态判定（none/single/multi）",
      prompt_gen.template_ref_mode(ENTRY) == "single"
      and prompt_gen.template_ref_mode(_e_none) == "none"
      and prompt_gen.template_ref_mode(_e_multi) == "multi"
      and prompt_gen.template_ref_mode(dict(ENTRY, strategy_type="base")) == "none")
# 多视图模板：单图源 = 扩展推演；多图源 = 视角对齐参考图
_p3t = prompt_gen.pick_variations(42, tpl3)
_i3_single = prompt_gen.build_local_instruction(ENTRY, _p3t)
check("三视图模板 + 单参考图：以单图为基准补全其余视角",
      "补全其余视角" in _i3_single and "同一个角色" in _i3_single,
      _i3_single[-150:])
_i3_multi = prompt_gen.build_local_instruction(_e_multi, _p3t)
check("三视图模板 + 多参考图：各视角与对应参考图一致",
      "对应的参考图" in _i3_multi, _i3_multi[-150:])
# 元提示词同步分流；三视图模板不再自相矛盾地带「收敛为单张立绘」
_m3_single = prompt_gen.build_meta_prompt(ENTRY, _p3t)
check("三视图模板元提示词（单图源）不再带收敛单图条款",
      "收敛为单张立绘" not in _m3_single and "补全其余视角" in _m3_single)
_m3_multi = prompt_gen.build_meta_prompt(_e_multi, _p3t)
check("三视图模板元提示词（多图源）要求视角对齐参考图",
      "对应的参考图" in _m3_multi and "收敛为单张立绘" not in _m3_multi)
# 单图输出 + 多图源：以正面全身为主
_m1_multi = prompt_gen.build_meta_prompt(_e_multi, prompt_gen.pick_variations(42, tpl_single))
check("单图模板 + 多参考图：以正面全身那张为主",
      "正面全身那张为主" in _m1_multi, _m1_multi[:200])
# 单图输出 + 单图源：保留「多视角源收敛为单图」条款（防复刻拼接布局）
check("单图模板 + 单参考图：保留收敛条款",
      "只保留其中一个视角" in prompt_gen.build_local_instruction(
          ENTRY, prompt_gen.pick_variations(42, tpl_single)))
# 无参考图：不提「根据参考图」、不带多视角源收敛条款
_i_none = prompt_gen.build_local_instruction(_e_none, prompt_gen.pick_variations(42))
check("无参考图指令不提参考图", "参考图" not in _i_none, _i_none[:120])
_i_none0 = prompt_gen.build_local_instruction(
    _e_none, {k: {"zh": "", "en": ""} for k in prompt_gen.DIMS})
check("无参考图且无变化的兜底指令改为直接生成",
      "参考图" not in _i_none0 and "直接生成" in _i_none0)
# img2img 缺源：生成链路与 draw.generate_portrait 同步降级文生图
async def _run_i2i_noref(seed):
    return await prompt_gen.build_generation_prompt(
        _BrokenContext(),
        {"prompt_llm_enabled": True, "prompt_llm_provider": "",
         "variations_enabled": True},
        dict(ENTRY, strategy={}), seed)

_nr_text, _nr_src = asyncio.run(_run_i2i_noref(7))
check("img2img 缺源降级文生图（不再发编辑指令）",
      _nr_src in ("local", "static") and "参考图" not in _nr_text, _nr_text[:80])
# 无参考图 + 三视图模板（t2i 链路）：tag 串带 character sheet 固定构图
_t2i_3v = prompt_gen.build_t2i_prompt(dict(ENTRY, strategy_type="base"), _p3t)
check("无参考图三视图模板走 t2i tag 串（character sheet 在）",
      "character sheet, three views" in _t2i_3v
      and "grey background" in _t2i_3v
      and "twintails" in _t2i_3v)

# ---------- v0.29.7：风格探测跟随实际工作流 + 底模库缺档案时按文件名猜 ----------
check("effective_workflow：条目覆盖优先于全局",
      prompt_gen.effective_workflow({"strategy": {"workflow": "qwen动画"}},
                                   {"draw_workflow": "全局工作流"}) == "qwen动画"
      and prompt_gen.effective_workflow({"strategy": {}},
                                        {"draw_workflow": "全局工作流"}) == "全局工作流"
      and prompt_gen.effective_workflow({}, {}) == "")


class _FakeStarMeta:
    def __init__(self, anima):
        self.star_cls = anima


class _FakeCtx:
    """带 get_registered_star 的 context 桩（返回 _FakeStarMeta 或 None）。"""

    def __init__(self, meta):
        self._meta = meta

    def get_registered_star(self, name):
        return self._meta


class _FakeBS:
    def __init__(self, rec):
        self._rec = rec

    def match_model(self, model_file="", class_type=""):
        return self._rec


class _FakeWS:
    def __init__(self, rec):
        self._rec = rec

    def get(self, wf_id, with_json=False):
        return self._rec


def _fake_ctx(wf_entry, wf_rec, bs_rec):
    class A:
        pass
    a = A()
    a.config = {"workflows": [wf_entry]}
    a.workflow_store = _FakeWS(wf_rec)
    a.basemodel_store = _FakeBS(bs_rec)
    return _FakeCtx(_FakeStarMeta(a))


_QWEN_WF = {"name": "qwen动画", "base_id": "3"}
_QWEN_REC = {"roles": {"model_file": "qwen_image_2.1_int8_convrot.safetensors"}}

check("探测：底模档案标了 qwen → qwen",
      prompt_gen.detect_t2i_style(
          _fake_ctx(_QWEN_WF, _QWEN_REC, {"prompt_style": "qwen"}),
          "qwen动画") == "qwen")
check("探测：底模库没标风格，模型文件名含 qwen → 猜 qwen",
      prompt_gen.detect_t2i_style(
          _fake_ctx(_QWEN_WF, _QWEN_REC, {"prompt_style": ""}),
          "qwen动画") == "qwen")
check("探测：底模库完全没建档，文件名含 qwen → 猜 qwen",
      prompt_gen.detect_t2i_style(
          _fake_ctx(_QWEN_WF, _QWEN_REC, None), "qwen动画") == "qwen")
check("探测：文件名含 moss → 猜 natural",
      prompt_gen.detect_t2i_style(
          _fake_ctx({"name": "moss", "base_id": "3"},
                    {"roles": {"model_file": "MOSS-T2I-v2.safetensors"}},
                    {"prompt_style": ""}), "moss") == "natural")
check("探测：认不出的底模 → danbooru",
      prompt_gen.detect_t2i_style(
          _fake_ctx({"name": "某工作流", "base_id": "3"},
                    {"roles": {"model_file": "whatever_v1.safetensors"}},
                    None), "某工作流") == "danbooru")
check("探测：工作流未绑基础库 → danbooru",
      prompt_gen.detect_t2i_style(
          _fake_ctx({"name": "旧版工作流", "base_id": ""}, None, None),
          "旧版工作流") == "danbooru")
check("探测：萌绘未装 → danbooru",
      prompt_gen.detect_t2i_style(_FakeCtx(None), "x") == "danbooru")

print("test_prompt_gen: ALL OK")
