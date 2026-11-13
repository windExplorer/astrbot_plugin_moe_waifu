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
例外（v0.19.3）：出图方式配置为 img2img（只用图生图模板）时不再受配额短路——
每次都按出卡源现画，库满也画（否则「只用图生图模板」永远不生效）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import tempfile
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


def _strategy_size(strategy: dict) -> tuple:
    """显式指定的图生图宽高（strategy.width/height，v0.13.3）。

    来源是工坊 img2img 面板的「宽/高」输入（经 effective_entry 写进 strategy），
    两者都合法（256~4096）才生效——只填一边视同未指定（尺寸是成对语义）。
    返回 (0, 0) = 未指定，调用方走 多视图自动尺寸 / 配置默认。
    """
    if not isinstance(strategy, dict):
        return 0, 0
    try:
        w = int(strategy.get("width") or 0)
        h = int(strategy.get("height") or 0)
    except (TypeError, ValueError):
        return 0, 0
    if w < 256 or h < 256 or w > 4096 or h > 4096:
        return 0, 0
    return w, h


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
        # v0.13.4：文生图链也传「尺寸开关」——绑定了尺寸来源开关的新版工作流
        # （ComfySwitchNode + EmptyLatentImage）上，只传宽高而 custom_size=None
        # 会按工作流条目配置（默认关）被忽略、回落工作流默认尺寸；显式传 True
        # 才让上面的配置宽高真正生效。旧版萌绘丢弃未知参数并 WARN，无开关的
        # 工作流忽略该请求（萌绘日志有说明），均无害。
        kwargs["custom_size"] = True
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


def explicit_ref(strategy: dict, data_dir=None) -> str:
    """**显式指定**的图生图源（strategy.ref）；未指定或文件失效返回空串。

    v0.10.0 起这是唯一能进入图生图的入口：参考图库 `refs` 只作资料库
    （试画时由用户手动选图再经 override 写进 ref），不参与自动选取。
    """
    if not isinstance(strategy, dict):
        return ""
    return _ref_path(strategy.get("ref"), data_dir)


def draw_source(entry: dict, data_dir=None) -> str:
    """出卡（/今日老婆）专用图生图源（strategy.draw_ref）。

    与「参考图库 / 主参考」是两回事（用户口径 v0.10.0）：只有管理员在工坊
    显式指定的这一张能用于生成老婆的图生图；没指定就不规划图生图、
    不发图生图台词、更不会去参考图列表里随便挑一张。
    """
    st = entry.get("strategy") or {}
    return _ref_path(st.get("draw_ref"), data_dir) if isinstance(st, dict) else ""


def has_draw_source(entry: dict, data_dir=None) -> bool:
    return bool(draw_source(entry, data_dir))


def _stage_refs(refs: list, data_dir) -> tuple[list, str]:
    """把参考图**复制**到 refs_stage/ 再把副本交给萌绘，返回 (副本列表, 临时目录)。

    萌绘图库归档是「移动转正」语义（`image_store.archive_image` 用
    `os.replace`），直接传本插件的 `data/refs/` 原图会被它搬进自己的图库，
    源文件消失——v0.11.0 多图参考实测：生成完成后几张参考图全变成
    「文件丢失」卡片（绑定还在、文件没了）。这里先复制一份，被搬走的是副本。
    URL 不需要落盘，原样透传。全部复制失败时返回 ([], "")。
    """
    if not data_dir:
        return list(refs), ""
    base = Path(data_dir) / "refs_stage"
    try:
        base.mkdir(parents=True, exist_ok=True)
        # 顺手清理上次异常残留（>1h 的 staging 目录）
        now = time.time()
        for d in base.iterdir():
            try:
                if d.is_dir() and now - d.stat().st_mtime > 3600:
                    shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass
        stage = Path(tempfile.mkdtemp(dir=str(base)))
    except Exception:
        return list(refs), ""
    out = []
    for i, p in enumerate(refs):
        s = str(p or "").strip()
        if not s:
            continue
        if re.match(r"^[a-zA-Z]+://", s):
            out.append(s)   # URL 交给萌绘自行下载
            continue
        src = Path(s)
        if not src.is_file():
            continue
        dst = stage / f"{i:02d}{(src.suffix or '.png').lower()}"
        try:
            shutil.copy2(src, dst)
            out.append(str(dst))
        except Exception:
            continue
    if not out:
        shutil.rmtree(stage, ignore_errors=True)
        return [], ""
    return out, str(stage)


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
    data_dir = cfg.get("data_dir") or None
    # v0.10.0：图生图源必须**显式指定**（strategy.ref / gen_refs；抽卡时由
    # _produce 把 strategy.draw_ref 注入到这里）。绝不再从参考图库 refs 按
    # 种子随机取图——「没指定图生图参考图，就不能私自用参考图列表里面的」。
    # v0.11.0：gen_refs 支持多图（工作流支持多图参考时按选择顺序喂）。
    ref = explicit_ref(strategy, data_dir)
    refs = gen_refs(strategy, data_dir)
    if ref and ref not in refs:
        refs.insert(0, ref)
    if stype == "img2img" and not refs:
        # 旧数据兜底：角色画法写着 img2img 却没指定源 → 按 tags 文生图，
        # 不中断出卡，也不会偷偷拿资料库里的图。
        stype = "base"
    timeout = int(cfg.get("draw_timeout", 120))
    if timeout <= 0:
        timeout = 120
    stage_dir = ""   # img2img 参考图 staging 目录（finally 里清理）

    if stype == "img2img":
        tool_name = "comfyui_img2img"
        handler = _get_tool_handler(context, tool_name)
        if handler is None:
            return None, _STATUS_NO_ANIMA, "未安装萌绘（comfyui_img2img 工具不存在）", 0
        image = refs[0]   # 已解析成绝对路径 / URL，且必然非空
        if not re.match(r"^[a-zA-Z]+://", image) and not Path(image).is_file():
            return None, "error", f"图生图源文件不存在：{image}", 0
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
        # v0.12.6：传 staging 副本（萌绘归档参考图会 os.replace 搬走源文件）
        staged, stage_dir = _stage_refs(refs, data_dir)
        if not staged:
            return None, "error", "图生图源文件不存在或无法读取", 0
        kwargs["image"] = staged[0]
        if len(staged) > 1:
            # 萌绘 v7.8.1+ 的 comfyui_img2img 支持多图；image 仍传第一张兼容旧版。
            kwargs["images"] = staged
        # denoise：条目 strategy.denoise 优先（逐角色调重绘强度）→ 全局配置兜底
        try:
            kwargs["denoise"] = float(
                strategy.get("denoise") if strategy.get("denoise") is not None
                else cfg.get("img2img_denoise", 0.65)
            )
        except (TypeError, ValueError):
            kwargs["denoise"] = 0.65
        # 尺寸（v0.13.3）：img2img **默认也传宽高** + 调用级尺寸开关
        # （萌绘 v7.7.100+ 的 custom_size）。旧版只有多视图底稿才传宽高，
        # 普通重绘输出尺寸跟参考图走——多视图源转单图会得到横版「单图」，
        # 正常立绘该用配置的 draw_width/height。优先级：
        #   本次显式指定（strategy.width/height，工坊 img2img 面板可填）
        #   > 多视图底稿自动横/方版 > 配置 draw_width/height。
        # 带「尺寸来源开关」的工作流（Qwen 2.1 Edit 等）按宽高出图；
        # 普通图生图工作流忽略宽高（仍跟参考图，萌绘日志有说明）；
        # 旧版萌绘（< v7.7.100）丢弃未知参数并 WARN，均无害。
        w_in, h_in = _strategy_size(strategy)
        if w_in and h_in:
            kwargs["width"], kwargs["height"] = _snap16(w_in), _snap16(h_in)
            kwargs["custom_size"] = True
        else:
            view_i2i = detect_view_layout(prompt)
            kwargs["custom_size"] = True
            if view_i2i:
                kwargs["width"], kwargs["height"] = _VIEW_SIZES[view_i2i]
            else:
                kwargs["width"] = _snap16(cfg.get("draw_width", 700))
                kwargs["height"] = _snap16(cfg.get("draw_height", 1200))
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
        try:
            raw = await asyncio.wait_for(handler(event, **kwargs), timeout=timeout)
        except TypeError as exc:
            # 旧版萌绘（< v7.8.1）的 comfyui_img2img 没有 images 参数：去掉多图
            # 重试一次单图，别让「多选参考图」直接把出图打挂。
            if "images" not in kwargs:
                return None, "error", f"TypeError: {exc}", int((time.monotonic() - t0) * 1000)
            kwargs.pop("images", None)
            try:
                raw = await asyncio.wait_for(handler(event, **kwargs), timeout=timeout)
            except asyncio.TimeoutError:
                return None, "timeout", f"萌绘生图超时（>{timeout}s）", int((time.monotonic() - t0) * 1000)
            except Exception as exc2:
                return None, "error", f"{exc2.__class__.__name__}: {exc2}", int((time.monotonic() - t0) * 1000)
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
    finally:
        # staging 副本：无论成功 / 超时 / 异常都清掉（被萌绘搬走的已不存在）
        if stage_dir:
            shutil.rmtree(stage_dir, ignore_errors=True)


def pick_ref(refs, seed_int: int, data_dir=None) -> str:
    """从参考图列表按种子选一张（确定性随机：同种子同图；失效的自动跳过）。

    refs 元素允许绝对路径、相对 data 目录路径或 URL；只回传真实可用的条目。
    ⚠️ v0.10.0 起**出图不再调用它**：参考图列表只是资料库，图生图源必须由
    用户显式指定（explicit_ref / draw_source），不能自动替用户挑一张。
    """
    candidates = usable_refs({"refs": refs}, data_dir)
    if not candidates:
        return ""
    return candidates[int(seed_int) % len(candidates)]


def gen_refs(strategy: dict, data_dir=None) -> list:
    """本次图生图的**多图源**（strategy.gen_refs，按用户选择顺序）。

    与参考图库 `strategy.refs` 是两回事：后者只是资料库，这里只放用户在
    工坊里显式勾选、本次要喂给工作流的图（Qwen 2.1 等多图工作流按
    「图1/图2/图3」顺序注入）。失效文件自动跳过、去重。
    """
    if not isinstance(strategy, dict):
        return []
    raw = strategy.get("gen_refs")
    out = []
    for x in (raw if isinstance(raw, list) else []):
        p = _ref_path(x, data_dir)
        if p and p not in out:
            out.append(p)
    return out


def has_gen_refs(entry: dict, data_dir=None) -> bool:
    """是否有可用的图生图源（显式 gen_refs 多图，或旧的主参考 ref 单图）。"""
    st = entry.get("strategy") or {}
    if not isinstance(st, dict):
        return False
    return bool(gen_refs(st, data_dir) or explicit_ref(st, data_dir))


def effective_entry(entry: dict, override: dict) -> dict:
    """试画台临时参数 → 生效条目（深拷贝，绝不改原条目）。

    override: {strategy_type?: base|lora|img2img, ref?, refs?: [str], denoise?,
               img2img_workflow?, loras?: [str], width?, height?}
    仅覆盖本次用到的字段；img2img 时清掉条目自带的 img2img_workflow（跟随本次输入/
    全局配置），未传 denoise 则沿用条目/全局。`refs` 是**本次多图源**（写进
    `strategy.gen_refs`，不碰资料库 `strategy.refs`）。
    width/height（v0.13.3）：本次图生图显式尺寸（工坊 img2img 面板「宽/高」，
    留空 = 跟随配置）；成对生效，写进 strategy 供 _strategy_size 读取。
    """
    import json as _json

    e = _json.loads(_json.dumps(entry, ensure_ascii=False))
    st = dict(e.get("strategy") or {}) if isinstance(e.get("strategy"), dict) else {}
    t = str((override or {}).get("strategy_type") or "").strip().lower()
    if t in ("base", "lora", "img2img"):
        st["type"] = t
        e["strategy_type"] = t
    if st.get("type") == "img2img":
        refs_in = (override or {}).get("refs")
        refs = ([str(x).strip() for x in refs_in if str(x or "").strip()]
                if isinstance(refs_in, list) else [])
        ref = str((override or {}).get("ref") or "").strip()
        if ref and ref not in refs:
            refs.insert(0, ref)
        if refs:
            st["gen_refs"] = refs
            st["ref"] = refs[0]      # 兼容只认单张 ref 的旧链路
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
        # v0.13.3：本次显式尺寸（成对生效；只填一边 = 未指定，弹出旧值防残留）
        try:
            w_in = int((override or {}).get("width") or 0)
            h_in = int((override or {}).get("height") or 0)
        except (TypeError, ValueError):
            w_in = h_in = 0
        if w_in >= 256 and h_in >= 256 and w_in <= 4096 and h_in <= 4096:
            st["width"], st["height"] = w_in, h_in
        else:
            st.pop("width", None)
            st.pop("height", None)
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
                pool_size: int, quota: int, mode: str = "") -> str:
    """出图决策（纯函数，可测）：reuse_today / generate / from_pool。

    v0.19.3：`mode`（配置 gen_source_mode）必须参与判定。旧实现只按配额走
    （< 配额 → generate，满配额 → from_pool），于是「只用图生图模板」在立绘库
    攒满之后被「满配额取库」短路——用户实报：配了 img2img，抽卡却一直是库里的
    旧立绘、再没画过新图。语义修正：
      - img2img（只用图生图模板）→ 每次都走 generate（是否真能图生图交给
        plan_source：没指定出卡源才回退取库）；
      - 其它模式（mix / pool）→ 维持原「集满配额就取图、不浪费算力」。
    `mode` 默认空串 = 老行为（debug 模拟等未传的旧调用点安全）。
    """
    if str(mode) == "img2img":
        # v0.19.6：img2img（只用图生图模板）连「当日复用」也不参与——该模式的
        # 语义是「每次都按出卡源现画」。旧实现今天只要生成过一张同角色的立绘，
        # 后面所有抽签（含重置后再抽）都返回 reuse_today，用户实报
        # 「后台显示当日复用立绘，没有图生图了」。
        return "generate"
    if today_row is not None:
        return "reuse_today"
    if pool_size < max(1, int(quota)):
        return "generate"
    return "from_pool"


def has_template_ref(entry: dict, data_dir=None) -> bool:
    """是否有**显式指定**的图生图源（strategy.ref，供试画 / 补图守卫用）。

    v0.10.0：参考图库 `strategy.refs` 只是资料库，不再算作「有模板图」。
    抽卡出图的专用源另见 `has_draw_source`（strategy.draw_ref）。
    """
    st = entry.get("strategy") or {}
    return bool(explicit_ref(st, data_dir))


def plan_source(mode: str, pool_size: int, quota: int, has_ref: bool,
                roll: int, i2i_prob: int) -> str:
    """出图方式规划（纯函数，可测，v0.9.6）：from_pool / generate_i2i / generate。

    三种模式（配置 gen_source_mode）：
    - mix：按 roll（种子派生 0~99）与 i2i_prob 在「库存取图」与「模板图生图」
      间分配——概率命中图生图但没绑模板图 → 回退库存取图；
    - pool：只从立绘库取图（库存空 → 按角色画法生成，老链路）；
    - img2img：只用模板图生图（没绑模板图 → 回退库存取图，库存也空 → 老链路）。
    ⚠️ v0.19.3：满配额短路只对 mix / pool 生效——img2img 的语义就是「每次都按
    出卡源现画」，不能被库存已满短路掉（用户实报的「配置了只用图生图模板却一直
    取立绘」）；代价是 img2img 模式下立绘库会随抽卡持续增长。
    generate_i2i 产出照常归档进立绘库。
    """
    if str(mode) == "img2img":
        return "generate_i2i" if has_ref else ("from_pool" if pool_size > 0 else "generate")
    if pool_size >= max(1, int(quota)):
        return "from_pool"
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


def explain_decision(mode: str, decision: str, pool_size: int, quota: int,
                     has_ref: bool, i2i_prob: int = 50, roll: int = 0,
                     today_portrait: bool = False) -> str:
    """把「这次为什么这么出图」翻成一句可读的中文原因（纯函数，可测）。

    v0.19.4：用户实报「配置只用图生图模板却一直取立绘」——只靠最终 decision
    字段（generate / from_pool / reuse_today）说不出原因，回退链有三条完全不同的
    岔路（满配额、没指定出卡源、概率没命中）。这里把决策与前提一起写成说明，
    随签落库（draws.decision_note）并进日志，工坊记录页可直接看。

    不抛异常，任何脏输入都返回一句「—」级别的兜底，绝不阻塞出卡。
    """
    try:
        m = str(mode or "mix")
        d = str(decision or "")
        quota = max(1, int(quota or 1))
        pool_size = max(0, int(pool_size or 0))
        full = pool_size >= quota
        stock = f"库存 {pool_size}/{quota}"
        if d == "reuse_today":
            return ("该角色今天就生成过立绘 → 直接复用当日那张（同角色同一天人人同款，"
                    "省算力）；想重画用「重试今日老婆立绘」，或把出图方式改成"
                    "「只用图生图模板」")
        if m == "img2img":
            if d == "generate_i2i":
                base = ("出图方式=只用图生图模板 → 按出卡源图生图"
                        + ("（库存已满，该模式仍现画）" if full else f"（{stock}，未满配额）"))
                if today_portrait:
                    base += "；当日该角色已有立绘，img2img 模式不共享、仍现画"
                return base
            if d == "from_pool":
                return ("出图方式=只用图生图模板，但该角色未指定「出卡图生图源」"
                        f" → 回退立绘库取图（{stock}）")
            return ("出图方式=只用图生图模板，但既没有出卡源、立绘库也为空"
                    " → 按角色画法生成（文生图/LoRA）")
        if m == "pool":
            if d == "from_pool":
                return f"出图方式=只用立绘库 → 从库存取图（{stock}）"
            return "出图方式=只用立绘库，但库存为空 → 按角色画法生成（文生图/LoRA）"
        # mix（含未知 mode 按 mix 处理）
        roll, prob = int(roll or 0) % 100, max(0, min(100, int(i2i_prob or 0)))
        if d == "generate_i2i":
            return (f"出图方式=混合，种子掷点 {roll} < 图生图概率 {prob}%"
                    f" → 按出卡源图生图（{stock}）")
        if d == "from_pool":
            if full:
                return (f"出图方式=混合，立绘库已满配额（{stock}）"
                        " → 直接取库存，不再消耗绘图算力")
            if not has_ref:
                return (f"出图方式=混合，掷点 {roll} 本可命中图生图（<{prob}%），"
                        "但该角色未指定「出卡图生图源」 → 回退立绘库取图")
            return (f"出图方式=混合，种子掷点 {roll} ≥ 图生图概率 {prob}%"
                    f" → 从立绘库取图（{stock}）")
        return ("出图方式=混合，立绘库为空且没有出卡源"
                " → 按角色画法生成（文生图/LoRA）")
    except Exception:  # pragma: no cover - 说明文字绝不能影响出卡
        return ""


def log_exception(prefix: str) -> str:
    return f"{prefix}\n{traceback.format_exc()}"
