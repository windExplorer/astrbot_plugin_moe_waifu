# -*- coding: utf-8 -*-
"""萌萌今日老婆 · 萌绘（astrbot_plugin_comfyui_anima）调用封装 + 立绘库决策。

契约（萌绘 docs/cross-plugin-draw-guide.md §3，v7.7.58+；星语 main.py 同款实装）：
- get_llm_tool_manager().get_func("comfyui_draw" / "comfyui_img2img") → handler；
- external=True 请萌绘「返回图片路径、由我方发图」（同时默认静默）；
- source 只填本插件标识（萌绘日志/审计用，不兼任行为开关）；
- seed / width / height 透传；动漫角色用英文 danbooru tag；raw_prompt=True 跳过
  萌绘侧的 LLM 润色/翻译（我们的 tags_en 是库里精修好的，不需要再处理）。
- 图生图尺寸（v0.7.3）：多视图底稿（三/四视图）会额外传 width/height + custom_size
  （萌绘 v7.7.100+ 的调用级尺寸开关；图生图顶层宽高需萌绘 v7.8.0+）。
  旧版萌绘会丢弃未知参数并打 WARN，功能不受影响，只是图生图尺寸不生效。
- **硬依赖**：未装萌绘 / 超时 / 异常 → 报错不出卡（PRD 铁律 1），不做无图降级。

立绘库决策（PRD §5.2）：当日已生成 → 复用；< 配额 → 生成并存档；满配额 → 取模取库存图。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import time
import traceback
from pathlib import Path

PLUGIN_SOURCE = "萌萌今日老婆"

# 内置负面兜底：排除男性特征 + NSFW（星语 DEFAULT_DRAW_NEGATIVE 的角色卡子集）。
DEFAULT_DRAW_NEGATIVE = (
    "1boy, male, man, boy, masculine, male focus, multiple views, multiple girls, "
    "nsfw, lowres, bad anatomy, bad hands, worst quality, low quality, "
    "watermark, signature, text, logo"
)

# 构图基础词：单人立绘（非外观特征，可与角色 tag 共存）
_PROMPT_BASE = "1girl, solo, best quality, looking at viewer"

# ---- 多视图底稿（character sheet）尺寸决策（v0.7.3）----
# 三视图（正/侧/背横排）与四视图（多角度同框）是横/方形构图——沿用竖版 700×1200
# 会把几个人挤进瘦长条，躯干四肢全变形。按行业惯例给：
#   三视图 → 1216×832（横版，每人约 400px 宽，够画清全身）
#   四视图 → 1024×1024（方形，模型通常自发排 2×2 网格）
# 「正常站姿全身」（tpl_standing）仍是竖版立绘，用配置的 draw_width/height。
_VIEW_SIZES = {"3view": (1216, 832), "4view": (1024, 1024)}

# 识别正则：中英都认（文生图走英文 tags，图生图的 LLM 编辑指令是中文）。
# 四视图先判——它的英文串里也有 multiple views，但 three views/三视图 只属于三视图。
_VIEW_4_RE = re.compile(r"four views|四视图|multiple views", re.IGNORECASE)
_VIEW_3_RE = re.compile(r"three views|三视图|front view[，,、\s]*side view", re.IGNORECASE)

# 多视图底稿时要从负面里剥掉的词：multiple views / multiple girls 会跟「同框多个
# 视图」直接打架（模型大概率被压回单人图），检测到视图词就剥掉，其余保留。
_VIEW_NEG_STRIP = {"multiple views", "multiple girls"}

_STATUS_NO_ANIMA = "skipped_no_anima"  # 界面文案「萌绘未安装」；历史落库枚举


def detect_view_layout(prompt: str) -> str:
    """从提示词识别多视图底稿类型（v0.7.3）。返回 "3view" / "4view" / ""。

    文生图（英文 tags）与图生图（中文 LLM 编辑指令）都能命中；
    普通立绘（含「正面朝向镜头」这类单视图描述）不会被误判。
    """
    text = str(prompt or "")
    if _VIEW_4_RE.search(text):
        return "4view"
    if _VIEW_3_RE.search(text):
        return "3view"
    return ""


def _snap16(v) -> int:
    """对齐到 16 的倍数（扩散模型latent 的习惯步长，最小 64）。"""
    try:
        return max(64, int(round(int(v) / 16)) * 16)
    except (TypeError, ValueError):
        return 1024


def _first_sentence(raw: str, limit: int = 80) -> str:
    """取萌绘失败文本的首句（萌绘失败时会附一句给 LLM 的提示语，别原样吐给用户）。"""
    text = str(raw or "").strip()
    if not text:
        return "萌绘未返回结果"
    m = re.split(r"[。\n]", text)
    first = (m[0] if m else text).strip()
    return first[:limit] if first else text[:limit]


def parse_draw_response(raw) -> list:
    """解析萌绘 comfyui_draw/img2img 的返回文本 → 本地图片路径列表。

    external=True 时返回 JSON 文本 {"image_paths": [...], "status": "ok"}
    （兼容旧单数 image_path 字段）；容错：截取花括号段再解析，失败返回空表。
    （星语 fortune.parse_draw_response 同款。）
    """
    text = str(raw or "")
    data = {}
    try:
        loaded = json.loads(text)
        if isinstance(loaded, dict):
            data = loaded
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                loaded = json.loads(m.group(0))
                if isinstance(loaded, dict):
                    data = loaded
            except Exception:
                data = {}
    paths = data.get("image_paths") or []
    if not paths and data.get("image_path"):
        paths = [data["image_path"]]
    out = []
    for p in paths:
        p = str(p or "").strip()
        if p:
            out.append(p)
    return out


def build_prompt(entry: dict) -> str:
    """本地拼装提示词：库里精修好的 tags_en + 构图基础词，不过 LLM（PRD §5.1）。

    外观 tag 若与萌绘侧角色卡锚点冲突会被萌绘自动剥离，无害。
    """
    tags = str(entry.get("tags_en") or "").strip().rstrip(",")
    return f"{_PROMPT_BASE}, {tags}" if tags else _PROMPT_BASE


def resolve_negative(cfg: dict, prompt: str = "") -> str:
    if not bool(cfg.get("draw_negative_enabled", True)):
        return ""
    neg = str(cfg.get("draw_negative_prompt") or "").strip() or DEFAULT_DRAW_NEGATIVE
    # 多视图底稿：负面里的 multiple views / multiple girls 会压制「同框多视图」，
    # 检测到视图词就剥掉这两条（其余排除项保留）（v0.7.3）
    if detect_view_layout(prompt):
        parts = [t.strip() for t in neg.split(",")]
        parts = [t for t in parts if t and t.lower() not in _VIEW_NEG_STRIP]
        neg = ", ".join(parts)
    return neg


def _get_tool_handler(context, tool_name: str):
    """拿萌绘 LLM 工具 handler；萌绘未装返回 None。"""
    try:
        manager = context.get_llm_tool_manager()
        tool = manager.get_func(tool_name) if manager is not None else None
    except Exception:
        return None
    if tool is None:
        return None
    return getattr(tool, "handler", None) or tool


def _base_kwargs(cfg: dict, prompt: str, seed_int: int,
                 with_workflow: bool = True, with_size: bool = True) -> dict:
    kwargs = dict(
        prompt=prompt,
        source=PLUGIN_SOURCE,
        external=True,
        seed=int(seed_int),
        silent=True,
        raw_prompt=True,
    )
    if with_size:
        # 多视图底稿 → 横/方形专用尺寸；普通立绘 → 配置尺寸（对齐 16，
        # 700×1200 这类非 16 步长的配置值会对齐成 704×1200，对模型更友好）
        view = detect_view_layout(prompt)
        if view:
            kwargs["width"], kwargs["height"] = _VIEW_SIZES[view]
        else:
            kwargs["width"] = _snap16(cfg.get("draw_width", 700))
            kwargs["height"] = _snap16(cfg.get("draw_height", 1200))
    workflow = str(cfg.get("draw_workflow") or "").strip()
    if with_workflow and workflow:
        kwargs["workflow"] = workflow
    negative = resolve_negative(cfg, prompt)
    if negative:
        kwargs["negative_prompt"] = negative
    return kwargs


def _ref_path(raw, data_dir=None) -> str:
    """把一条参考图引用解析成可直接交给萌绘的绝对路径 / URL，失效返回空串。

    工坊里 refs 混着三种形态：绝对路径（上传）、相对 data 目录的
    `refs/xxx.png`（立绘→参考图、绑定）、URL。旧代码用 `Path(ref).exists()`
    按**进程 CWD** 判断，相对路径在 AstrBot 里永远找不到；而规划器又只查
    「字符串非空」，于是「没绑图生图源」的签被规划成图生图、发了图生图
    台词，最后在 generate_portrait 里找不到参考图。这里统一口径。
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    if re.match(r"^[a-zA-Z]+://", s):   # URL 交给萌绘，本地不校验
        return s
    p = Path(s)
    if not p.is_absolute() and data_dir:
        p = Path(data_dir) / p
    try:
        return str(p.resolve()) if p.is_file() else ""
    except OSError:
        return ""


def usable_refs(strategy: dict, data_dir=None) -> list:
    """策略里**真实可用**的参考图（主参考优先、去重），已解析成绝对路径 / URL。"""
    if not isinstance(strategy, dict):
        return []
    raw_refs = strategy.get("refs")
    raws = [strategy.get("ref")] + (
        list(raw_refs) if isinstance(raw_refs, list) else [])
    out = []
    for raw in raws:
        p = _ref_path(raw, data_dir)
        if p and p not in out:
            out.append(p)
    return out


async def generate_portrait(context, cfg: dict, entry: dict, seed_int: int,
                            event=None, prompt: str | None = None):
    """按角色策略生成立绘。返回 (本地路径|None, status, error, duration_ms)。

    ⚠️ 所有分支必须返回**4 元组**：调用方一律 `a, b, c, d = await ...`，
    少一个元素就是 `not enough values to unpack`（v0.9.9 实测事故）。
    event 传真实指令事件（萌绘内部会用到会话上下文；WebUI 试画等无事件场景留 None，
    由调用方自行兜底）。status：ok / skipped_no_anima / timeout / error。失败时路径
    为 None——调用方必须报错不出卡，绝不做无图降级（PRD 铁律 1）。
    prompt：v0.5.6 起由调用方经 prompt_gen 预生成（LLM 编辑指令 / 含变化维度的
    tags）；留 None 时回落本地 tags 拼装（build_prompt，兼容旧路径）。
    cfg["data_dir"]：参考图里相对路径（refs/xxx.png）的解析基准。
    """
    prompt = str(prompt or "").strip() or build_prompt(entry)
    stype = str(entry.get("strategy_type") or "base")
    strategy = entry.get("strategy") or {}
    timeout = int(cfg.get("draw_timeout", 120))
    if timeout <= 0:
        timeout = 120

    if stype == "img2img":
        tool_name = "comfyui_img2img"
        handler = _get_tool_handler(context, tool_name)
        if handler is None:
            return None, _STATUS_NO_ANIMA, "未安装萌绘（comfyui_img2img 工具不存在）", 0
        # 参考图解析（v0.9.9）：主参考 strategy.ref 优先 → refs 按种子确定性选；
        # 统一经 usable_refs 解析成绝对路径 / URL（相对路径按 cfg["data_dir"] 找），
        # 与 has_template_ref 的规划判定完全同口径，杜绝「说有图、画时找不到」。
        data_dir = cfg.get("data_dir") or None
        main_ref = _ref_path(strategy.get("ref"), data_dir)
        refs = usable_refs(strategy, data_dir)
        ref = main_ref or (refs[int(seed_int) % len(refs)] if refs else "")
        if not ref:
            return None, "error", (
                "图生图没有可用参考图（文件不存在或已被清理）：请在工坊重新上传，"
                "或把该角色的画法改回文生图"), 0
        image = ref
        if not re.match(r"^[a-zA-Z]+://", image) and not Path(image).is_file():
            return None, "error", f"参考图文件不存在：{image}", 0
        # img2img 不带 workflow（那是文生图槽位）；图生图工作流三级解析：
        # 条目 strategy.img2img_workflow → 全局配置 img2img_workflow → 留空（萌绘默认）
        kwargs = _base_kwargs(cfg, prompt, seed_int,
                              with_workflow=False, with_size=False)
        img_workflow = (
            str(strategy.get("img2img_workflow") or "").strip()
            or str(cfg.get("img2img_workflow") or "").strip()
        )
        if img_workflow:
            kwargs["img2img_workflow"] = img_workflow
        kwargs["image"] = image
        # denoise：条目 strategy.denoise 优先（逐角色调重绘强度）→ 全局配置兜底
        try:
            kwargs["denoise"] = float(
                strategy.get("denoise") if strategy.get("denoise") is not None
                else cfg.get("img2img_denoise", 0.65)
            )
        except (TypeError, ValueError):
            kwargs["denoise"] = 0.65
        # 多视图底稿（v0.7.3）：横/方形尺寸 + 调用级尺寸开关（萌绘 v7.7.100+）。
        # 带「尺寸来源开关」的工作流（Qwen 2.1 Edit 等）→ 拨开关注入宽高；
        # 普通图生图工作流 → 宽高被忽略（尺寸仍跟参考图，萌绘日志有说明），
        # 旧版萌绘（< v7.7.100）→ 未知参数被丢弃并 WARN，均无害。
        view_i2i = detect_view_layout(prompt)
        if view_i2i:
            kwargs["width"], kwargs["height"] = _VIEW_SIZES[view_i2i]
            kwargs["custom_size"] = True
    else:
        tool_name = "comfyui_draw"
        handler = _get_tool_handler(context, tool_name)
        if handler is None:
            return None, _STATUS_NO_ANIMA, "未安装萌绘（comfyui_draw 工具不存在）", 0
        kwargs = _base_kwargs(cfg, prompt, seed_int)
        # 条目级工作流覆盖（v0.7.2）：strategy.workflow 优先于全局 draw_workflow
        # （补一张/试画的工作流下拉经 effective_entry 写到这里；img2img 分支不用它）
        st_workflow = str(strategy.get("workflow") or "").strip()
        if st_workflow:
            kwargs["workflow"] = st_workflow
        if stype == "lora":
            loras = strategy.get("loras")
            if isinstance(loras, list) and loras:
                kwargs["loras"] = [str(x) for x in loras if str(x).strip()]
            else:
                return None, "error", "lora 策略缺少 loras 列表（如 [\"xxx:0.8\"]）", 0

    t0 = time.monotonic()
    try:
        raw = await asyncio.wait_for(handler(event, **kwargs), timeout=timeout)
    except asyncio.TimeoutError:
        return None, "timeout", f"萌绘生图超时（>{timeout}s）", int((time.monotonic() - t0) * 1000)
    except Exception as exc:
        return None, "error", f"{exc.__class__.__name__}: {exc}", int((time.monotonic() - t0) * 1000)
    duration = int((time.monotonic() - t0) * 1000)

    paths = parse_draw_response(raw)
    if not paths:
        # 萌绘失败时的返回是一句话（如「本次生图失败。请用一句话简短…」），
        # 只取首句 + 提示看萌绘日志，不再把整段提示语拼进错误里
        return None, "error", (
            f"萌绘侧生成失败：{_first_sentence(raw)}（请检查所选图生图工作流与"
            "参考图是否匹配，详见萌绘日志）"), duration
    first = Path(paths[0])
    if not first.exists():
        return None, "error", f"萌绘返回的路径不存在: {paths[0]}", duration
    return str(first), "ok", "", duration


def pick_ref(refs, seed_int: int, data_dir=None) -> str:
    """从参考图列表按种子选一张（确定性随机：同种子同图；失效的自动跳过）。

    refs 元素允许绝对路径、相对 data 目录路径或 URL；只回传真实可用的条目。
    """
    candidates = usable_refs({"refs": refs}, data_dir)
    if not candidates:
        return ""
    return candidates[int(seed_int) % len(candidates)]


def effective_entry(entry: dict, override: dict) -> dict:
    """试画台临时参数 → 生效条目（深拷贝，绝不改原条目）。

    override: {strategy_type?: base|lora|img2img, ref?, denoise?, img2img_workflow?, loras?: [str]}
    仅覆盖试画本次用到的字段；img2img 时清掉条目自带的 img2img_workflow（跟随本次输入/
    全局配置），未传 denoise 则沿用条目/全局。
    """
    import json as _json

    e = _json.loads(_json.dumps(entry, ensure_ascii=False))
    st = dict(e.get("strategy") or {}) if isinstance(e.get("strategy"), dict) else {}
    t = str((override or {}).get("strategy_type") or "").strip().lower()
    if t in ("base", "lora", "img2img"):
        st["type"] = t
        e["strategy_type"] = t
    if st.get("type") == "img2img":
        ref = str((override or {}).get("ref") or "").strip()
        if ref:
            st["ref"] = ref
        dn = (override or {}).get("denoise")
        if dn is not None:
            try:
                st["denoise"] = float(dn)
            except (TypeError, ValueError):
                pass
        wf = str((override or {}).get("img2img_workflow") or "").strip()
        if wf:
            st["img2img_workflow"] = wf
        else:
            st.pop("img2img_workflow", None)
    if st.get("type") == "lora":
        loras = (override or {}).get("loras")
        if isinstance(loras, list) and loras:
            st["loras"] = [str(x) for x in loras if str(x).strip()]
    # 条目级工作流覆盖（v0.7.2）：补一张选了工作流时按条目画法透传——
    # base/lora 吃 workflow（generate_portrait base 分支读取）；img2img 条目
    # 用的是 img2img_workflow，此键对其无作用
    wf = str((override or {}).get("workflow") or "").strip()
    if wf:
        st["workflow"] = wf
    e["strategy"] = st
    return e


def workflow_img2img_capable(entry: dict, base_roles: dict | None = None) -> bool:
    """判断工作流能否图生图（用户口径 v0.3.6，替代 comfyui_workflows 的失真标记）。

    新版萌绘（工作流绑定了基础工作流库，base_id 非空）节点定位「以解析为准」，
    配置条目上的 image_node 字段不参与——所以能不能图生图要看**基础工作流的
    解析注记**里有没有图节点（base_roles["image_node"]，由调用方从
    anima.workflow_store 查询传入）；
    未绑定基础工作流的旧版工作流：名字里带「图生图」即视为图生图专用；
    手填 image_node 的旧配置显式可用。
    绑定了但解析注记查询失败（base_roles=None）时按可用兜底，不误伤。
    """
    if str(entry.get("image_node") or "").strip():
        return True
    base_id = str(entry.get("base_id") or "").strip()
    if base_id:
        if base_roles is None:
            return True
        return bool(str(base_roles.get("image_node") or "").strip())
    return "图生图" in str(entry.get("name") or "")


def archive_portrait(src: str, data_dir: Path, char_id: str, date_str: str,
                     seq: int, seed_int: int) -> str:
    """把萌绘产出的图复制进本插件立绘库，返回相对 data 目录的归档路径。

    萌绘的图存在它自己的 gallery 里——那份文件不可控（清理/版本升级），
    立绘库必须持有自己的拷贝（PRD §5.2：data 目录唯一真源）。
    """
    dest_dir = Path(data_dir) / "portraits" / char_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = f"{date_str}_{int(seq):03d}_{int(seed_int) % 100000:05d}.png"
    dest = dest_dir / name
    if dest.is_file():
        # 同角色 / 同日 / 同 seq 必然算出同名（删过立绘行后 seq 会被复用，
        # 抽卡与工坊补图同日也容易撞）。绝不覆盖旧图：内容相同就复用，
        # 不同则按内容寻址另起文件名，避免「旧立绘被悄悄换掉 / 少一张」。
        try:
            raw_new, raw_old = Path(src).read_bytes(), dest.read_bytes()
        except OSError:
            raw_new, raw_old = b"", b""
        if raw_new and raw_new == raw_old:
            return f"portraits/{char_id}/{name}"
        digest = hashlib.sha256(raw_new or str(time.time()).encode()).hexdigest()[:8]
        name = f"{date_str}_{int(seq):03d}_{int(seed_int) % 100000:05d}_{digest}.png"
        dest = dest_dir / name
    try:
        shutil.copy2(src, dest)
    except shutil.SameFileError:
        pass
    return f"portraits/{char_id}/{name}"


def resolve_portrait_rel(data_dir: Path, rel: str) -> Path:
    return Path(data_dir) / rel


def decision_of(char_id: str, date_str: str, today_row: dict | None,
                pool_size: int, quota: int) -> str:
    """出图决策（纯函数，可测）：reuse_today / generate / from_pool。"""
    if today_row is not None:
        return "reuse_today"
    if pool_size < max(1, int(quota)):
        return "generate"
    return "from_pool"


def has_template_ref(entry: dict, data_dir=None) -> bool:
    """角色是否绑定了**真实可用**的图生图模板图（文件存在 / URL）。

    v0.9.9 前只查「字符串非空」：工坊把相对路径 `refs/xxx.png` 或已被清掉的
    图留在 refs 里时，规划器会误以为有模板图 → 决策 generate_i2i → 误发
    「照着官方图精心重绘」提示 → 生图时找不到参考图。判定必须和真正出图时
    的 usable_refs 同口径，相对路径按 data_dir 解析。
    """
    st = entry.get("strategy") or {}
    return bool(usable_refs(st, data_dir))


def plan_source(mode: str, pool_size: int, quota: int, has_ref: bool,
                roll: int, i2i_prob: int) -> str:
    """出图方式规划（纯函数，可测，v0.9.6）：from_pool / generate_i2i / generate。

    三种模式（配置 gen_source_mode）：
    - mix：按 roll（种子派生 0~99）与 i2i_prob 在「库存取图」与「模板图生图」
      间分配——概率命中图生图但没绑模板图 → 回退库存取图；
    - pool：只从立绘库取图（库存空 → 按角色画法生成，老链路）；
    - img2img：只用模板图生图（没绑模板图 → 回退库存取图，库存也空 → 老链路）。
    满配额一律库存取图（不浪费算力）；generate_i2i 产出照常归档进立绘库。
    """
    if pool_size >= max(1, int(quota)):
        return "from_pool"
    if str(mode) == "img2img":
        return "generate_i2i" if has_ref else ("from_pool" if pool_size > 0 else "generate")
    if str(mode) == "pool":
        return "from_pool" if pool_size > 0 else "generate"
    # mix（非法 mode 也当 mix）
    want_i2i = has_ref and (int(roll) % 100) < max(0, min(100, int(i2i_prob)))
    if want_i2i:
        return "generate_i2i"
    if pool_size > 0:
        return "from_pool"
    # 库存空必须现画：绑了模板图就走图生图（稳），否则按角色画法（文生图/LoRA）
    return "generate_i2i" if has_ref else "generate"


def log_exception(prefix: str) -> str:
    return f"{prefix}\n{traceback.format_exc()}"
