# -*- coding: utf-8 -*-
"""萌萌今日老婆 · 立绘提示词生成（变化维度池 + 可选 LLM 通道）。

背景（v0.5.6）：图生图主用 Qwen-Image-Edit（单图编辑模型），它的提示词是
「自然语言编辑指令」而非 danbooru tag。官方指南要点：一句话里「动作 + 目标 +
约束」——明确说清改什么，并在句尾强调什么必须保持不变（面部特征 / 发色 /
辨识度），否则结果漂移；复杂改动宁可聚焦少数几处。

两个通道：
- LLM 通道（默认开）：把角色信息 + 本轮变化维度（种子决定）交给所配对话模型，
  产一条 Qwen-Image-Edit 编辑指令；失败/超时 → 本地模板兜底，绝不阻塞出卡。
- 本地模板：无 astrbot 依赖的确定性拼装（可独立测试）。

变化维度（outfit/background/pose/expression/body）由种子决定论派生：
同角色同天第 seq 张恒定（重试不换装）。body 维度大权重保持原样，
小概率触发身高/体型/胸部变化（用户明确要的「萝莉变高挑」一类）。
"""
from __future__ import annotations

import asyncio
import hashlib
import re

try:  # AstrBot 运行期
    from astrbot.api import logger
except Exception:  # pragma: no cover - 本地平铺调试
    import logging

    logger = logging.getLogger("moe_waifu")

# ---------- 变化维度池 ----------
# 每项 {"w": 权重, "zh": 中文短语（编辑指令用）, "en": 英文 tag（文生图用）}
# zh 为空 = 无需提及该项；en 为空 = 文生图不追加 tag。

OUTFITS = [
    {"w": 9, "zh": "水手服JK制服", "en": "sailor school uniform, serafuku"},
    {"w": 7, "zh": "白色婚纱", "en": "white wedding dress, bridal veil"},
    {"w": 7, "zh": "黑色旗袍", "en": "black qipao, china dress"},
    {"w": 6, "zh": "连体泳装", "en": "one-piece swimsuit"},
    {"w": 4, "zh": "分体泳装", "en": "bikini swimsuit"},
    {"w": 7, "zh": "干练的职业西装套裙", "en": "business suit, office lady"},
    {"w": 6, "zh": "日式浴衣", "en": "yukata, japanese clothes"},
    {"w": 6, "zh": "女仆装", "en": "maid outfit, maid headdress"},
    {"w": 5, "zh": "运动外套与短裤", "en": "track jacket, gym clothes"},
    {"w": 5, "zh": "冬季大衣与围巾", "en": "winter coat, scarf"},
    {"w": 6, "zh": "碎花夏日连衣裙", "en": "floral summer dress"},
    {"w": 5, "zh": "哥特萝莉装", "en": "gothic lolita dress, frills"},
    {"w": 5, "zh": "宽松针织毛衣与长裙", "en": "oversized sweater, long skirt"},
    {"w": 5, "zh": "演唱会打歌服", "en": "idol costume, stage outfit"},
    {"w": 4, "zh": "女巫斗篷与尖顶帽", "en": "witch hat, cape, mage outfit"},
    {"w": 4, "zh": "宽松居家睡衣", "en": "pajamas, loungewear"},
]

BACKGROUNDS = [
    {"w": 8, "zh": "纯白色摄影棚背景（柔光箱打光）", "en": "simple background, white background, studio lighting"},
    {"w": 7, "zh": "浅粉色纯色背景", "en": "simple background, pink background"},
    {"w": 6, "zh": "浅灰渐变摄影棚背景", "en": "simple background, grey background, soft lighting"},
    {"w": 6, "zh": "樱花树下的春日午后", "en": "cherry blossoms, outdoors, sunlight"},
    {"w": 6, "zh": "教室窗边（午后斜阳）", "en": "classroom, window, afternoon sunlight"},
    {"w": 5, "zh": "黄昏时分的街道", "en": "sunset, street, outdoors"},
    {"w": 5, "zh": "星空下的夜晚屋顶", "en": "night, stars, rooftop"},
    {"w": 5, "zh": "夏日海边（蓝天白云）", "en": "beach, ocean, summer, blue sky"},
    {"w": 5, "zh": "霓虹闪烁的都市夜景", "en": "city lights, night, neon lights"},
    {"w": 5, "zh": "绿意花园（树影间洒落的阳光）", "en": "garden, flowers, dappled sunlight"},
    {"w": 4, "zh": "雨天的窗边（室内）", "en": "rain, window, indoors"},
    {"w": 5, "zh": "暖色调的咖啡馆内景", "en": "cafe, indoors, warm lighting"},
    {"w": 4, "zh": "传统和室榻榻米", "en": "japanese room, tatami"},
    {"w": 4, "zh": "圣诞夜的彩灯街道", "en": "christmas lights, night, festive"},
]

POSES = [
    {"w": 7, "zh": "双手在胸前比出爱心", "en": "heart hands"},
    {"w": 6, "zh": "回头看向镜头", "en": "looking back, looking at viewer"},
    {"w": 6, "zh": "双臂抱胸、自信地站立", "en": "arms crossed, confident pose"},
    {"w": 6, "zh": "单手撩动头发", "en": "hand in own hair"},
    {"w": 6, "zh": "抬手挥手打招呼", "en": "waving"},
    {"w": 5, "zh": "双手捧住脸颊", "en": "hands on own cheeks"},
    {"w": 5, "zh": "怀里抱着毛绒玩偶", "en": "holding plush toy"},
    {"w": 5, "zh": "手里拿着一杯奶茶", "en": "holding cup, holding drink"},
    {"w": 5, "zh": "双手背在身后、身体微微前倾", "en": "arms behind back, leaning forward"},
    {"w": 4, "zh": "侧过身闭眼感受微风", "en": "side view, closed eyes, wind"},
]

EXPRESSIONS = [
    {"w": 8, "zh": "温柔微笑", "en": "smile"},
    {"w": 6, "zh": "害羞脸红", "en": "blush, shy"},
    {"w": 5, "zh": "得意的坏笑", "en": "smirk, smug"},
    {"w": 5, "zh": "惊讶地睁大眼睛", "en": "surprised, wide-eyed"},
    {"w": 5, "zh": "无语地半眯着眼", "en": "expressionless, half-closed eyes"},
    {"w": 5, "zh": "俏皮地眨眼吐舌", "en": "wink, tongue out"},
    {"w": 5, "zh": "安静若有所思", "en": "pensive"},
    {"w": 5, "zh": "开怀大笑", "en": "laughing, open mouth"},
    {"w": 4, "zh": "傲娇地嘟起嘴", "en": "pout"},
    {"w": 4, "zh": "困倦惺忪", "en": "sleepy"},
]

# body：大权重保持原样（体型变化对图生图一致性冲击最大），小概率触发用户要的
# 身高/体型/胸部变化。zh 为空串 = 指令与 tag 都不提及。
BODY = [
    {"w": 66, "zh": "", "en": ""},
    {"w": 8, "zh": "身材高挑的成熟体型（成年女性比例）", "en": "tall female, mature female"},
    {"w": 8, "zh": "娇小圆润的体型（幼态比例）", "en": "petite"},
    {"w": 7, "zh": "胸部更丰满一些", "en": "large breasts"},
    {"w": 7, "zh": "胸部更纤细一些", "en": "small breasts"},
    {"w": 4, "zh": "健身后的紧致身材", "en": "toned, athletic"},
]

DIMS = {
    "outfit": OUTFITS,
    "background": BACKGROUNDS,
    "pose": POSES,
    "expression": EXPRESSIONS,
    "body": BODY,
}


# ---------- 种子派生（决定论：同种子同维度同选择） ----------

def _sub(seed_int: int, tag: str, modulus: int) -> int:
    if modulus <= 0:
        return 0
    raw = f"{int(seed_int)}|{tag}".encode("utf-8")
    digest = hashlib.sha256(raw).digest()
    return int.from_bytes(digest[:8], "big") % modulus


def _weighted_pick(items: list, r: int) -> dict:
    total = sum(int(it.get("w", 1)) for it in items) or 1
    x = int(r) % total
    for it in items:
        x -= int(it.get("w", 1))
        if x < 0:
            return it
    return items[-1]


def pick_variations(seed_int: int) -> dict:
    """按种子从各维度池确定性挑选（纯函数，可测）。返回 {维度: 条目}。"""
    return {tag: _weighted_pick(items, _sub(seed_int, tag, 2 ** 30))
            for tag, items in DIMS.items()}


def variation_zh_list(picks: dict) -> list:
    """本轮要改的中文维度短语（跳过「保持不变」的空项），LLM 元提示用。"""
    order = [("服装", "outfit"), ("背景与光照", "background"),
             ("姿势动作", "pose"), ("表情", "expression"), ("体型", "body")]
    out = []
    for label, tag in order:
        zh = str((picks.get(tag) or {}).get("zh") or "").strip()
        if zh:
            out.append((label, zh))
    return out


# ---------- 本地模板（无 LLM 兜底） ----------

def build_local_instruction(entry: dict, picks: dict) -> str:
    """img2img 用：本地拼一条 Qwen-Image-Edit 风格的自然语言编辑指令。

    结构遵循官方指南「动作 + 目标 + 约束」：先列改动，句尾统一强调保持不变。
    """
    parts = []
    for label, zh in variation_zh_list(picks):
        if label == "服装":
            parts.append(f"把角色的服装换成{zh}")
        elif label == "背景与光照":
            parts.append(f"把背景改为{zh}")
        elif label == "姿势动作":
            parts.append(f"让角色{zh}")
        elif label == "表情":
            parts.append(f"表情改为{zh}")
        elif label == "体型":
            parts.append(f"把体型调整为{zh}（面部特征保持可辨认）")
    tail = ("。保持人物的面部特征、发型发色与角色辨识度不变，只做上述改动；"
            "输出单人完整竖版立绘")
    if not parts:
        return ("根据参考图重新绘制该角色的单人完整竖版立绘"
                "，保持人物的面部特征、发型发色与角色辨识度不变")
    return "，".join(parts) + tail


def build_t2i_prompt(entry: dict, picks: dict) -> str:
    """base/lora（文生图）用：原有 tags 拼装 + 变化维度的英文 tag 追加。"""
    base = "1girl, solo, best quality, looking at viewer"
    tags = str(entry.get("tags_en") or "").strip().rstrip(",")
    extra = []
    for tag in ("outfit", "background", "pose", "expression", "body"):
        en = str((picks.get(tag) or {}).get("en") or "").strip().strip(",")
        if en:
            extra.append(en)
    segs = [base]
    if tags:
        segs.append(tags)
    if extra:
        segs.append(", ".join(extra))
    return ", ".join(segs)


# ---------- LLM 通道 ----------

_SYSTEM_PROMPT = (
    "你为 Qwen-Image-Edit（通义千问图像编辑模型）撰写图生图编辑指令。"
    "该模型用自然语言做单图编辑，写法要点：一句话内「动作+目标+约束」，"
    "句尾强调保持人物面部特征、发型发色与角色辨识度不变；避免一次塞太多改动。"
    "输出纪律：只输出一条简体中文编辑指令本身——不要解释、不要引号、"
    "不要 markdown、不要分点，总长不超过 110 字。"
)


def build_meta_prompt(entry: dict, picks: dict) -> str:
    name = str(entry.get("name") or "").strip() or "该角色"
    series = str(entry.get("series") or "").strip() or "未知作品"
    tags = str(entry.get("tags_en") or "").strip() or "无"
    lines = [
        f"角色：{name}（出自 {series}），特征 tags：{tags}。",
        "参考图是该角色的官方立绘（单人）。本轮由抽签决定的改动维度：",
    ]
    zh_list = variation_zh_list(picks)
    if zh_list:
        lines += [f"- {label}：{zh}" for label, zh in zh_list]
    else:
        lines.append("- （全部维度保持不变）")
    lines += [
        "请把以上改动自然融合成一条编辑指令；未列出的维度一律不要改动。",
        "角色是二次元角色，目标是单人完整竖版立绘。",
    ]
    return "\n".join(lines)


def sanitize_llm_output(text: str, limit: int = 150) -> str:
    """LLM 返回清洗：剥 markdown/引号/前缀废话，取首个非空行，限长。"""
    s = str(text or "").strip()
    if not s:
        return ""
    s = re.sub(r"```[a-zA-Z]*", "", s).replace("```", "")
    for line in s.splitlines():
        line = line.strip()
        if not line:
            continue
        s = line
        break
    s = s.strip("\"'“”「」『』《》`*_ ")
    s = re.sub(r"^(编辑指令|指令|提示词|prompt)\s*[:：]\s*", "", s, flags=re.I)
    # 前缀剥掉后开头可能又露出引号（如「编辑指令：“…”」），再 strip 一次
    s = s.strip("\"'“”「」『』《》`*_ ")
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit] if s else ""


async def _resolve_provider(context, provider_id: str):
    """按配置解析对话模型实例：显式 ID → 默认提供商 → 任一 chat provider。"""
    pid = str(provider_id or "").strip()
    if pid:
        try:
            p = context.get_provider_by_id(pid)
        except Exception:
            p = None
        if p is not None and hasattr(p, "text_chat"):
            return p
        return None
    try:
        p = await context.get_using_provider_async()
        if p is not None and hasattr(p, "text_chat"):
            return p
    except Exception:
        pass
    try:
        provs = context.get_all_providers() or []
    except Exception:
        provs = []
    for p in provs:
        if hasattr(p, "text_chat"):
            return p
    return None


async def build_generation_prompt(context, cfg: dict, entry: dict,
                                  seed_int: int) -> tuple:
    """产出生图提示词。返回 (prompt, source)；source ∈ llm / local / static。

    绝不抛异常——LLM 链路任何失败都回落本地模板（提示词不是阻塞出卡的理由）。
    strategy_type=base/lora（文生图）走 tag 拼装；img2img 走编辑指令。
    """
    try:
        stype = str(entry.get("strategy_type") or "base")
        if stype != "img2img":
            return build_t2i_prompt(entry, pick_variations(seed_int)), "local"
        if not bool(cfg.get("variations_enabled", True)):
            return build_prompt_tags(entry), "static"
        picks = pick_variations(seed_int)
        if not bool(cfg.get("prompt_llm_enabled", True)):
            return build_local_instruction(entry, picks), "local"
        provider = await _resolve_provider(context, cfg.get("prompt_llm_provider", ""))
        if provider is None:
            logger.warning("[moe_waifu] 未找到可用的对话模型，提示词回落本地模板")
            return build_local_instruction(entry, picks), "local"
        timeout = int(cfg.get("prompt_llm_timeout", 30) or 30)
        if timeout <= 0:
            timeout = 30
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=build_meta_prompt(entry, picks),
                    session_id=None,
                    system_prompt=_SYSTEM_PROMPT,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(f"[moe_waifu] 提示词 LLM 超时（>{timeout}s），回落本地模板")
            return build_local_instruction(entry, picks), "local"
        text = sanitize_llm_output(getattr(resp, "completion_text", "") or "")
        if text:
            return text, "llm"
        logger.warning("[moe_waifu] 提示词 LLM 返回为空，回落本地模板")
        return build_local_instruction(entry, picks), "local"
    except Exception:
        logger.error("[moe_waifu] 提示词生成异常，回落本地模板", exc_info=True)
        try:
            if str(entry.get("strategy_type") or "base") == "img2img":
                return (build_local_instruction(
                    entry, pick_variations(seed_int)), "local")
            return build_prompt_tags(entry), "static"
        except Exception:
            return "1girl, solo, best quality, looking at viewer", "static"


def build_prompt_tags(entry: dict) -> str:
    """关闭变化维度时的原样 tag 拼装（与 draw.build_prompt 等价，避免循环依赖）。"""
    base = "1girl, solo, best quality, looking at viewer"
    tags = str(entry.get("tags_en") or "").strip().rstrip(",")
    return f"{base}, {tags}" if tags else base
