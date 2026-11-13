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


def check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    print(f"ok: {name}")


class _BrokenContext:
    """无 astrbot 环境桩：任何探测都抛错。"""

    def get_provider_by_id(self, pid):
        raise RuntimeError("no astrbot")


ENTRY = {"id": "miku", "name": "初音未来", "series": "Vocaloid",
         "tags_en": "hatsune miku, twintails", "strategy_type": "img2img"}

# ---------- pick_variations：决定论 ----------
p1 = prompt_gen.pick_variations(12345)
p2 = prompt_gen.pick_variations(12345)
check("同种子同选择", p1 == p2)
check("七个维度齐全", set(p1.keys()) == {"compose", "artstyle", "outfit", "background",
                                     "pose", "expression", "body"})
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
check("qwen 兜底是中文长描述", "竖版" in qwl and "。" in qwl)

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

# ---------- dim_pool_options：维度下拉选项（v0.14.0） ----------
pools = prompt_gen.dim_pool_options()
check("下拉池覆盖六维", set(pools.keys()) == set(prompt_gen.DIMS.keys()))
check("服装下拉无空项且降序", "" not in pools["outfit"]
      and len(pools["outfit"]) >= 50
      and int(next(it["w"] for it in prompt_gen.OUTFITS
                   if it["zh"] == pools["outfit"][0])) >= int(next(it["w"] for it in prompt_gen.OUTFITS
                                                                  if it["zh"] == pools["outfit"][-1])))

# ---------- 立绘模板主题 + 全身姿势池（v0.7.0；v0.7.4 起模板恢复随机穿搭） ----------
tpl3 = prompt_gen.get_theme("tpl_3view")
tpl4 = prompt_gen.get_theme("tpl_4view")
tpl_s = prompt_gen.get_theme("tpl_standing")
check("三套立绘模板主题存在", tpl3 is not None and tpl4 is not None and tpl_s is not None)
check("模板主题已不再置空服装", all("outfit" not in (t.get("plain_dims") or [])
                                  for t in prompt_gen.THEMES))
pool_zh = {str(it.get("zh") or "") for it in prompt_gen.OUTFITS}


def in_pool(zh: str) -> bool:
    """服装条目可能带颜色前缀（v0.13.1），只要款式部分在池内即可。"""
    return any(z and z in str(zh or "") for z in pool_zh)


for seed in (1, 42, 777, 99999):
    p3 = prompt_gen.pick_variations(seed, tpl3)
    check(f"三视图模板强制三视图(seed={seed})", "三视图" in p3["compose"]["zh"])
    check(f"三视图模板强制标准站姿(seed={seed})", "标准站姿" in p3["pose"]["zh"])
    check(f"三视图模板强制微笑(seed={seed})", "微笑" in p3["expression"]["zh"])
    check(f"三视图模板随机穿搭在池内(seed={seed})", in_pool(p3["outfit"]["zh"]))
    check(f"三视图模板强制灰底(seed={seed})", "灰" in p3["background"]["zh"])
p4 = prompt_gen.pick_variations(42, tpl4)
check("四视图模板强制四视图", "四视图" in p4["compose"]["zh"])
check("四视图模板随机穿搭在池内", in_pool(p4["outfit"]["zh"]))
ps = prompt_gen.pick_variations(42, tpl_s)
check("站姿模板强制正常站姿构图", "正常站姿" in ps["compose"]["zh"])
check("站姿模板随机穿搭在池内", in_pool(ps["outfit"]["zh"]))

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
      and "多视角拼接" in meta and "收敛为单张全身立绘" in meta)

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
      "整体画风为清透水彩绘本" in prompt_gen.build_t2i_qwen_local(ENTRY, _p_az))
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
# 内容主题都带画风约束（用户要求「主题里要加上画风」）
_no_style = [t["id"] for t in prompt_gen.THEMES
             if t["id"] != "random" and not (t.get("artstyle_kw") or [])]
check("除 random 外所有主题都约束画风", not _no_style)
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
_dv = prompt_gen.describe_variations(777)
check("describe_variations 与 variation_zh_list 同源",
      _dv == prompt_gen.variation_zh_list(prompt_gen.pick_variations(777)))
_dv_i2i = prompt_gen.describe_variations(777, force_single=True)
check("describe_variations force_single 不含三/四视图",
      all("视图" not in zh for _, zh in _dv_i2i))

print("test_prompt_gen: ALL OK")
