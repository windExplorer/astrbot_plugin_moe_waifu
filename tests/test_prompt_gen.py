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
check("六个维度齐全", set(p1.keys()) == {"compose", "outfit", "background",
                                     "pose", "expression", "body"})
check("维度取自池内", p1["outfit"] in prompt_gen.OUTFITS
      and p1["background"] in prompt_gen.BACKGROUNDS
      and p1["body"] in prompt_gen.BODY
      and p1["compose"] in prompt_gen.COMPOSE)

# 分布非退化：1000 个种子里服装至少出现 8 种（排除全撞同一项的退化）
seen = set()
for seed in range(1000):
    seen.add(id(prompt_gen.pick_variations(seed)["outfit"]))
check("服装维度分布非退化", len(seen) >= 8)

# body 大权重保持：BODY[0] 权重 66/100 ≈ 2/3
keep = prompt_gen.BODY[0]
keep_n = sum(1 for s in range(2000)
             if prompt_gen.pick_variations(s)["body"].get("zh") == "")
check("体型维度大权重保持原样", 1100 <= keep_n <= 1600)

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
                        ("expression_kw", "expression")):
        for kw in t.get(kw_key) or []:
            if not any(kw in str(it.get("zh") or "") for it in prompt_gen.DIMS[dim]):
                check(f"主题 {t['id']} 关键词命中池", False, f"{kw_key}:{kw}")
check("主题关键词全部命中池", True)

p_rand = prompt_gen.pick_variations(12345)
p_th = prompt_gen.pick_variations(12345, sw)
check("主题后服装在泳装子集", "泳装" in p_th["outfit"]["zh"])
check("主题后背景在海边子集", "海边" in p_th["background"]["zh"])
check("主题不影响其它维度", p_th["pose"] == p_rand["pose"]
      and p_th["compose"] == p_rand["compose"])
check("主题决定论", prompt_gen.pick_variations(12345, sw) == p_th)
check("随机模式与主题版不同（子集过滤生效）", p_th["outfit"] != p_rand["outfit"])

# ---------- 立绘模板主题 + 全身姿势池（v0.7.0；v0.7.4 起模板恢复随机穿搭） ----------
tpl3 = prompt_gen.get_theme("tpl_3view")
tpl4 = prompt_gen.get_theme("tpl_4view")
tpl_s = prompt_gen.get_theme("tpl_standing")
check("三套立绘模板主题存在", tpl3 is not None and tpl4 is not None and tpl_s is not None)
check("模板主题已不再置空服装", all("outfit" not in (t.get("plain_dims") or [])
                                  for t in prompt_gen.THEMES))
pool_zh = {str(it.get("zh") or "") for it in prompt_gen.OUTFITS}
for seed in (1, 42, 777, 99999):
    p3 = prompt_gen.pick_variations(seed, tpl3)
    check(f"三视图模板强制三视图(seed={seed})", "三视图" in p3["compose"]["zh"])
    check(f"三视图模板强制标准站姿(seed={seed})", "标准站姿" in p3["pose"]["zh"])
    check(f"三视图模板强制微笑(seed={seed})", "微笑" in p3["expression"]["zh"])
    check(f"三视图模板随机穿搭在池内(seed={seed})", p3["outfit"]["zh"] in pool_zh)
    check(f"三视图模板强制灰底(seed={seed})", "灰" in p3["background"]["zh"])
p4 = prompt_gen.pick_variations(42, tpl4)
check("四视图模板强制四视图", "四视图" in p4["compose"]["zh"])
check("四视图模板随机穿搭在池内", p4["outfit"]["zh"] in pool_zh)
ps = prompt_gen.pick_variations(42, tpl_s)
check("站姿模板强制正常站姿构图", "正常站姿" in ps["compose"]["zh"])
check("站姿模板随机穿搭在池内", ps["outfit"]["zh"] in pool_zh)

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

print("test_prompt_gen: ALL OK")
