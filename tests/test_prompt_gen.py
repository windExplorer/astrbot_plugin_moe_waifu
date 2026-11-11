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
check("五个维度齐全", set(p1.keys()) == {"outfit", "background", "pose", "expression", "body"})
check("维度取自池内", p1["outfit"] in prompt_gen.OUTFITS
      and p1["background"] in prompt_gen.BACKGROUNDS
      and p1["body"] in prompt_gen.BODY)

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
