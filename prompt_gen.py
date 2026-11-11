# -*- coding: utf-8 -*-
"""萌萌今日老婆 · 立绘提示词生成（变化维度池 + 可选 LLM 通道）。

背景（v0.5.6）：图生图主用 Qwen-Image-2.1（通义千问图像模型，文生图与图像
编辑统一在同一模型里，编辑由自然语言指令驱动）。其提示词是「自然语言编辑指令」
而非 danbooru tag——写法要点（官方指南）：动作 + 目标 + 约束，一句话里说清
「改什么」并强调「什么保持不变」，否则结果漂移。

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

# ---------- 主题预设（试画台「第 2 步：提示词」用） ----------
# outfit_kw / bg_kw：从对应维度池里按中文子串筛子集，再在该子集内按种子加权；
# 筛不出子集（池里没这个词）就退回全池随机。custom 由用户在预览面板自填。
THEMES = (
    {"id": "random", "label": "随机（按种子变化）", "outfit_kw": [], "bg_kw": []},
    {"id": "swimsuit", "label": "泳装 · 夏日", "outfit_kw": ["泳装"], "bg_kw": ["海边"]},
    {"id": "campus", "label": "校园 · JK", "outfit_kw": ["水手服", "运动外套"], "bg_kw": ["教室", "樱花"]},
    {"id": "wedding", "label": "婚纱 · 典礼", "outfit_kw": ["婚纱"], "bg_kw": ["纯白", "浅粉"]},
    {"id": "office", "label": "职场 · 通勤", "outfit_kw": ["西装", "毛衣"], "bg_kw": ["咖啡馆", "霓虹"]},
    {"id": "kimono", "label": "和风 · 浴衣", "outfit_kw": ["浴衣"], "bg_kw": ["和室", "樱花"]},
    {"id": "gothic", "label": "暗黑 · 哥特", "outfit_kw": ["哥特", "女巫"], "bg_kw": ["霓虹", "雨"]},
    {"id": "home", "label": "居家 · 慵懒", "outfit_kw": ["睡衣", "毛衣"], "bg_kw": ["雨", "和室", "咖啡馆"]},
    {"id": "idol", "label": "偶像 · 舞台", "outfit_kw": ["打歌服"], "bg_kw": ["圣诞", "霓虹", "星空"]},
    {"id": "party", "label": "旗袍 · 东方", "outfit_kw": ["旗袍"], "bg_kw": ["和室", "樱花", "花园"]},
)


def get_theme(theme_id: str) -> dict | None:
    tid = str(theme_id or "").strip()
    if not tid or tid == "random":
        return None
    return next((t for t in THEMES if t["id"] == tid), None)


def apply_theme(picks: dict, seed_int: int, theme: dict | None) -> dict:
    """主题约束：outfit / background 在主题子集内重摇（其余维度保持种子随机）。

    纯函数：同 (seed, theme) 恒定；子集为空时不动（回全池结果）。
    """
    if not theme:
        return picks
    for dim, kw_key in (("outfit", "outfit_kw"), ("background", "bg_kw")):
        kws = [str(k).strip() for k in (theme.get(kw_key) or []) if str(k).strip()]
        if not kws:
            continue
        subset = [it for it in DIMS[dim]
                  if any(kw in str(it.get("zh") or "") for kw in kws)]
        if subset:
            picks[dim] = _weighted_pick(
                subset, _sub(seed_int, f"{dim}@{theme.get('id')}", 2 ** 30))
    return picks


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


def pick_variations(seed_int: int, theme: dict | None = None) -> dict:
    """按种子从各维度池确定性挑选（纯函数，可测）。返回 {维度: 条目}。

    theme 传入主题预设时，outfit / background 在主题子集内重摇。
    """
    picks = {tag: _weighted_pick(items, _sub(seed_int, tag, 2 ** 30))
             for tag, items in DIMS.items()}
    return apply_theme(picks, seed_int, theme)


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

def build_local_instruction(entry: dict, picks: dict, custom: str = "") -> str:
    """img2img 用：本地拼一条 Qwen-Image-2.1 风格的自然语言编辑指令。

    结构遵循官方指南「动作 + 目标 + 约束」：先列改动，句尾统一强调保持不变。
    custom 为用户在预览面板自填的附加要求，作为一条改动子句并入。
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
    extra = str(custom or "").strip().strip("。，,")
    if extra:
        parts.append(extra)
    tail = ("。保持人物的面部特征、发型发色与角色辨识度不变，只做上述改动；"
            "输出单人完整竖版立绘")
    if not parts:
        return ("根据参考图重新绘制该角色的单人完整竖版立绘"
                "，保持人物的面部特征、发型发色与角色辨识度不变")
    return "，".join(parts) + tail


def build_t2i_prompt(entry: dict, picks: dict) -> str:
    """danbooru 系底模（anima/illustrious/noobai/pony 等）用：tags 拼装 + 变化 tag。"""
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


def build_t2i_natural_local(entry: dict, picks: dict, custom: str = "") -> str:
    """自然语言系底模（Moss-Image/FLUX/krea 等）用：英文 caption 本地兜底。

    按 MOSS-Image 规范的 Tag 串顺序拼（Quality → Character/Series → General）：
    MOSS 三式混用皆可，本地不写长句是保底——真 caption 交给 LLM 通道。
    """
    segs = ["best quality, highres, solo, full body portrait"]
    tags = str(entry.get("tags_en") or "").strip().rstrip(",")
    if tags:
        segs.append(tags)
    for tag in ("outfit", "background", "pose", "expression", "body"):
        en = str((picks.get(tag) or {}).get("en") or "").strip().strip(",")
        if en:
            segs.append(en)
    extra = str(custom or "").strip().strip(",")
    if extra:
        segs.append(extra)
    return ", ".join(segs)


def build_t2i_qwen_local(entry: dict, picks: dict, custom: str = "") -> str:
    """Qwen-Image-2.1 文生图用：中文长描述本地兜底（官方格式是约 20 句长段，
    本地只能给个结构完整的短版，真长段交给 LLM 通道）。"""
    zh = [str((picks.get(k) or {}).get("zh") or "").strip().strip("。")
          for k in ("background", "outfit", "pose", "expression", "body")]
    extra = str(custom or "").strip().strip("。")
    if extra:
        zh.append(extra)
    zh = [s for s in zh if s]
    tags = str(entry.get("tags_en") or "").strip().rstrip(",")
    lines = ["一幅单人完整竖版角色立绘插画，构图干净、光影柔和、细节精致"]
    if zh:
        lines.append("；".join(zh))
    if tags:
        lines.append(f"角色外观特征（英文 tags）：{tags}")
    return "。".join(lines) + "。"


# ---------- LLM 通道 ----------

_SYSTEM_PROMPT = (
    "你为 Qwen-Image-2.1（通义千问图像模型，文生图与图像编辑统一在同一模型，"
    "编辑由自然语言指令驱动）撰写图生图编辑指令。"
    "写法要点：一句话内「动作+目标+约束」，句尾强调保持人物面部特征、"
    "发型发色与角色辨识度不变；避免一次塞太多改动。"
    "输出纪律：只输出一条简体中文编辑指令本身——不要解释、不要引号、"
    "不要 markdown、不要分点，总长不超过 110 字。"
)

# 文生图（natural 系底模，如 Moss-Image）：MOSS-Image 规范 A 档——英文 caption、
# 单主体一段描述、约 15~40 词、禁质量词堆砌、身份锚定逐项写明。
_SYSTEM_T2I_NATURAL = (
    "你为 Moss-Image（自然语言系文生图模型，英文效果最好）撰写单人角色立绘的"
    "英文提示词。写法：一段连贯的英文描述（caption），覆盖主体外观、服装、"
    "动作姿势、表情、场景与光照、构图（full body portrait）；角色身份锚定——"
    "把已给 tags 里的发色/发型/瞳色/标志性特征自然融入描述，点名用"
    "「规范英文名（作品名）」句式。禁止：8k/highly detailed 之类质量词堆砌"
    "（最多以 best quality, highres 开头）、markdown、解释性文字。"
    "输出纪律：只输出提示词本体，一段话，15~50 词，不超过 80 词。"
)

# 文生图（Qwen-Image-2.1）：官方格式——一段连贯长描述（本插件精简到 5~10 句），
# 中文优先（Qwen 原生中文最好），不写比例与画质套话。
_SYSTEM_T2I_QWEN = (
    "你为 Qwen-Image-2.1（通义千问图像模型，文生图吃官方格式的连贯长描述）"
    "撰写单人角色立绘提示词。写法：一段连贯的中文描述（5~10 句），覆盖角色"
    "外观（把已给 tags 的发色/发型/瞳色/服装特征写成自然语句）、服装、姿势、"
    "表情、背景环境与光影、构图（单人完整竖版立绘）；不写比例数字、"
    "不写 best quality 之类画质套话、不用 markdown、不写标签串。"
    "输出纪律：只输出提示词本体一段话，不要解释、不要引号、不要分点。"
)


def build_meta_prompt(entry: dict, picks: dict, custom: str = "") -> str:
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
    extra = str(custom or "").strip()
    if extra:
        lines.append(f"- 用户附加要求：{extra}")
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


def detect_t2i_style(context, workflow_name: str) -> str:
    """探测文生图工作流底模的提示词风格（danbooru / natural / qwen）。

    链路：萌绘 star_cls.config["workflows"] 找 workflow_name 条目 → base_id →
    workflow_store.get(base_id)["roles"]["model_file"] → basemodel_store.
    match_model(model_file)["prompt_style"]。任一环节失败（萌绘未装 / 工作流
    未绑基础库 / 查询异常）→ 回落 "danbooru"（本插件历史上一直按 tag 拼，
    对 tag 兼容底模最安全）。
    """
    try:
        meta = context.get_registered_star("astrbot_plugin_comfyui_anima")
        anima = getattr(meta, "star_cls", None)
        if anima is None:
            return "danbooru"
        cfg = getattr(anima, "config", None)
        raw = cfg.get("workflows") if isinstance(cfg, dict) else None
        wf = next((e for e in (raw or [])
                   if isinstance(e, dict)
                   and str(e.get("name") or "") == str(workflow_name or "").strip()
                   and str(e.get("base_id") or "").strip()), None)
        if wf is None:
            return "danbooru"
        store = getattr(anima, "workflow_store", None)
        roles = ((store.get(int(wf["base_id"])) or {}).get("roles") or {}) \
            if store is not None else {}
        model_file = str(roles.get("model_file") or "")
        bs = getattr(anima, "basemodel_store", None)
        rec = bs.match_model(model_file) if bs is not None and model_file else None
        style = str((rec or {}).get("prompt_style") or "").strip().lower()
        return style if style in ("danbooru", "natural", "qwen") else "danbooru"
    except Exception:
        return "danbooru"


def build_t2i_meta_prompt(entry: dict, picks: dict, style: str,
                          custom: str = "") -> str:
    name = str(entry.get("name") or "").strip() or "该角色"
    series = str(entry.get("series") or "").strip()
    tags = str(entry.get("tags_en") or "").strip() or "无"
    extra = str(custom or "").strip()
    if style == "qwen":
        head = f"角色：{name}" + (f"（出自 {series}）" if series else "")
        segs = [f"{head}。外观特征 tags（供参考，需转写成自然语句）：{tags}。"]
        segs += [f"{label}：{zh}" for label, zh in variation_zh_list(picks)]
        if extra:
            segs.append(f"用户附加要求：{extra}")
        segs.append("目标是单人完整竖版立绘，请写成一段连贯中文长描述。")
        return "\n".join(segs)
    # natural（MOSS 口径）
    head = f"Character: {name}" + (f" ({series})" if series else "")
    segs = [f"{head}. Identity tags (anchor these features verbatim): {tags}."]
    segs += [f"{label}: {zh}" for label, zh in variation_zh_list(picks)]
    if extra:
        segs.append(f"User extra requirement: {extra}")
    segs.append("Target: a single-character full-body portrait illustration. "
                "Write ONE connected English caption.")
    return "\n".join(segs)


def _t2i_local_of(style: str, entry: dict, picks: dict, custom: str = "") -> tuple:
    """分风格的文生图本地兜底 + 静态回退。"""
    if style == "natural":
        return build_t2i_natural_local(entry, picks, custom), "local"
    if style == "qwen":
        return build_t2i_qwen_local(entry, picks, custom), "local"
    return build_t2i_prompt(entry, picks), "local"


async def build_generation_prompt(context, cfg: dict, entry: dict,
                                  seed_int: int, theme_id: str = "",
                                  custom: str = "") -> tuple:
    """产出生图提示词。返回 (prompt, source)；source ∈ llm / local / static。

    绝不抛异常——LLM 链路任何失败都回落本地模板（提示词不是阻塞出卡的理由）。
    img2img（qwen image 2.1 编辑）走自然语言编辑指令；base/lora（文生图）按
    工作流底模风格分派：danbooru=tag 拼装、natural=英文 caption、qwen=中文
    长描述（风格探测失败一律回落 danbooru tag 拼装）。
    theme_id：主题预设（试画台预览用；random/空 = 纯种子随机）。
    custom：用户自填的附加要求（融入指令/描述）。
    """
    theme = get_theme(theme_id)
    try:
        stype = str(entry.get("strategy_type") or "base")
        if stype != "img2img":
            if not bool(cfg.get("variations_enabled", True)):
                return build_prompt_tags(entry), "static"
            picks = pick_variations(seed_int, theme)
            style = detect_t2i_style(context, cfg.get("draw_workflow", ""))
            if not bool(cfg.get("prompt_llm_enabled", True)) or style == "danbooru":
                return _t2i_local_of(style, entry, picks, custom)
            provider = await _resolve_provider(
                context, cfg.get("prompt_llm_provider", ""))
            if provider is None:
                logger.warning("[moe_waifu] 未找到可用的对话模型，提示词回落本地模板")
                return _t2i_local_of(style, entry, picks, custom)
            timeout = int(cfg.get("prompt_llm_timeout", 30) or 30)
            if timeout <= 0:
                timeout = 30
            system = _SYSTEM_T2I_QWEN if style == "qwen" else _SYSTEM_T2I_NATURAL
            try:
                resp = await asyncio.wait_for(
                    provider.text_chat(
                        prompt=build_t2i_meta_prompt(entry, picks, style, custom),
                        session_id=None,
                        system_prompt=system,
                    ),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(f"[moe_waifu] 提示词 LLM 超时（>{timeout}s），回落本地模板")
                return _t2i_local_of(style, entry, picks, custom)
            text = sanitize_llm_output(getattr(resp, "completion_text", "") or "",
                                       limit=400 if style == "qwen" else 300)
            if text:
                return text, "llm"
            logger.warning("[moe_waifu] 提示词 LLM 返回为空，回落本地模板")
            return _t2i_local_of(style, entry, picks, custom)
        if not bool(cfg.get("variations_enabled", True)):
            return build_prompt_tags(entry), "static"
        picks = pick_variations(seed_int, theme)
        if not bool(cfg.get("prompt_llm_enabled", True)):
            return build_local_instruction(entry, picks, custom), "local"
        provider = await _resolve_provider(context, cfg.get("prompt_llm_provider", ""))
        if provider is None:
            logger.warning("[moe_waifu] 未找到可用的对话模型，提示词回落本地模板")
            return build_local_instruction(entry, picks, custom), "local"
        timeout = int(cfg.get("prompt_llm_timeout", 30) or 30)
        if timeout <= 0:
            timeout = 30
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=build_meta_prompt(entry, picks, custom),
                    session_id=None,
                    system_prompt=_SYSTEM_PROMPT,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(f"[moe_waifu] 提示词 LLM 超时（>{timeout}s），回落本地模板")
            return build_local_instruction(entry, picks, custom), "local"
        text = sanitize_llm_output(getattr(resp, "completion_text", "") or "")
        if text:
            return text, "llm"
        logger.warning("[moe_waifu] 提示词 LLM 返回为空，回落本地模板")
        return build_local_instruction(entry, picks, custom), "local"
    except Exception:
        logger.error("[moe_waifu] 提示词生成异常，回落本地模板", exc_info=True)
        try:
            if str(entry.get("strategy_type") or "base") == "img2img":
                return (build_local_instruction(
                    entry, pick_variations(seed_int, theme), custom), "local")
            return build_prompt_tags(entry), "static"
        except Exception:
            return "1girl, solo, best quality, looking at viewer", "static"


def build_prompt_tags(entry: dict) -> str:
    """关闭变化维度时的原样 tag 拼装（与 draw.build_prompt 等价，避免循环依赖）。"""
    base = "1girl, solo, best quality, looking at viewer"
    tags = str(entry.get("tags_en") or "").strip().rstrip(",")
    return f"{base}, {tags}" if tags else base
