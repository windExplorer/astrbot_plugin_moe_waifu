# -*- coding: utf-8 -*-
"""萌萌今日老婆 · 萌绘（astrbot_plugin_comfyui_anima）调用封装 + 立绘库决策。

契约（萌绘 docs/cross-plugin-draw-guide.md §3，v7.7.58+；星语 main.py 同款实装）：
- get_llm_tool_manager().get_func("comfyui_draw" / "comfyui_img2img") → handler；
- external=True 请萌绘「返回图片路径、由我方发图」（同时默认静默）；
- source 只填本插件标识（萌绘日志/审计用，不兼任行为开关）；
- seed / width / height 透传；动漫角色用英文 danbooru tag；raw_prompt=True 跳过
  萌绘侧的 LLM 润色/翻译（我们的 tags_en 是库里精修好的，不需要再处理）。
- **硬依赖**：未装萌绘 / 超时 / 异常 → 报错不出卡（PRD 铁律 1），不做无图降级。

立绘库决策（PRD §5.2）：当日已生成 → 复用；< 配额 → 生成并存档；满配额 → 取模取库存图。
"""
from __future__ import annotations

import asyncio
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

_STATUS_NO_ANIMA = "skipped_no_anima"  # 界面文案「萌绘未安装」；历史落库枚举


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


def resolve_negative(cfg: dict) -> str:
    if not bool(cfg.get("draw_negative_enabled", True)):
        return ""
    return str(cfg.get("draw_negative_prompt") or "").strip() or DEFAULT_DRAW_NEGATIVE


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


def _base_kwargs(cfg: dict, prompt: str, seed_int: int, with_workflow: bool = True) -> dict:
    kwargs = dict(
        prompt=prompt,
        source=PLUGIN_SOURCE,
        external=True,
        seed=int(seed_int),
        width=int(cfg.get("draw_width", 700)),
        height=int(cfg.get("draw_height", 1200)),
        silent=True,
        raw_prompt=True,
    )
    workflow = str(cfg.get("draw_workflow") or "").strip()
    if with_workflow and workflow:
        kwargs["workflow"] = workflow
    negative = resolve_negative(cfg)
    if negative:
        kwargs["negative_prompt"] = negative
    return kwargs


async def generate_portrait(context, cfg: dict, entry: dict, seed_int: int, event=None):
    """按角色策略生成立绘。返回 (本地路径|None, status, error, duration_ms)。

    event 传真实指令事件（萌绘内部会用到会话上下文；WebUI 试画等无事件场景留 None，
    由调用方自行兜底）。status：ok / skipped_no_anima / timeout / error。失败时路径
    为 None——调用方必须报错不出卡，绝不做无图降级（PRD 铁律 1）。
    """
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
        ref = str(strategy.get("ref") or "").strip()
        if not ref:
            return None, "error", "img2img 策略缺少参考图（strategy.ref 未填）", 0
        image = ref
        if not re.match(r"^[a-zA-Z]+://", ref):  # 非协议开头 → 视为本地路径
            if not Path(ref).exists():
                return None, "error", f"参考图文件不存在：{ref}", 0
            image = str(Path(ref).resolve())
        # img2img 不带 workflow（那是文生图槽位）；图生图工作流三级解析：
        # 条目 strategy.img2img_workflow → 全局配置 img2img_workflow → 留空（萌绘默认）
        kwargs = _base_kwargs(cfg, build_prompt(entry), seed_int, with_workflow=False)
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
    else:
        tool_name = "comfyui_draw"
        handler = _get_tool_handler(context, tool_name)
        if handler is None:
            return None, _STATUS_NO_ANIMA, "未安装萌绘（comfyui_draw 工具不存在）", 0
        kwargs = _base_kwargs(cfg, build_prompt(entry), seed_int)
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
        return None, "error", f"萌绘未返回图片路径: {str(raw)[:150]}", duration
    first = Path(paths[0])
    if not first.exists():
        return None, "error", f"萌绘返回的路径不存在: {paths[0]}", duration
    return str(first), "ok", "", duration


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


def log_exception(prefix: str) -> str:
    return f"{prefix}\n{traceback.format_exc()}"
