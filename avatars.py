# -*- coding: utf-8 -*-
"""萌萌今日老婆 · 角色头像（v0.31.0）。

头像 = 方形小图，存在本插件数据目录 `avatars/<角色id>.png`，条目字段
`entry.avatar` 存相对 data 目录的路径（与 cover 同一引用口径）。

三条来源：
- 手动上传（工坊资料页 → 上传弹窗）→ 居中裁方形；
- 从已有图片取（立绘 / 参考图，点选即裁）；
- 工作流生成（萌绘）：有可用图生图源的角色走 comfyui_img2img
  （「只保留头部与肩部」的固定编辑指令，身份最稳），否则按条目画法
  走 comfyui_draw 文生图（按底模风格拼头像提示词）；产物同样裁方形。
  单个生成走 jobs 轮询（webui_api._run_avatar_job），批量走 AvatarRunner。

裁剪口径：竖版图取**偏上方**的正方形（人脸通常在画面上部 10%~40%），
横版图取水平居中正方形；统一缩到 512×512 存储（LANCZOS）。
"""
from __future__ import annotations

import asyncio
import time
import traceback
from pathlib import Path

try:  # AstrBot 运行期
    from astrbot.api import logger
except Exception:  # pragma: no cover - 本地平铺调试
    import logging

    logger = logging.getLogger("moe_waifu")

AVATAR_SIDE = 512
# 竖版图裁剪的顶部偏移比例（正方形上沿 = 高度差 × 该比例）。
# 头像特写类产物脸就在上半部，偏移取小；全身立绘的脸在 10%~40% 区间，
# 6% 偏移 + 整宽窗口能把头+肩都装进去。
_TOP_RATIO = 0.06

AVATAR_DIR = "avatars"


def safe_avatar_name(char_id: str) -> str:
    """角色 id → 安全文件名（id 一般是 slug，防御性再过滤一遍）。"""
    import re

    return re.sub(r"[^0-9A-Za-z._-]", "_", str(char_id or "").strip())[:60] or "char"


def avatar_rel(char_id: str) -> str:
    """条目 avatar 字段的标准相对路径。"""
    return f"{AVATAR_DIR}/{safe_avatar_name(char_id)}.png"


def crop_square(src, dst) -> tuple:
    """任意图片 → 方形头像（偏上裁剪 + 512×512），返回 (宽, 高)。

    失败抛异常（调用方决定如何报错）；Pillow 延迟导入（与 draw.image_size 同口径）。
    """
    from PIL import Image

    with Image.open(str(src)) as im:
        im = im.convert("RGBA")
        w, h = im.size
        side = min(w, h)
        if h > w:            # 竖版：偏上裁剪（保脸）
            y0 = min(max(0, int((h - side) * _TOP_RATIO)), h - side)
            box = (0, y0, w, y0 + side)
        elif w > h:          # 横版：水平居中
            x0 = (w - side) // 2
            box = (x0, 0, x0 + side, h)
        else:
            box = (0, 0, w, h)
        sq = im.crop(box)
        if sq.width != AVATAR_SIDE or sq.height != AVATAR_SIDE:
            sq = sq.resize((AVATAR_SIDE, AVATAR_SIDE), Image.LANCZOS)
        # PNG 带透明通道即可，背景交原图
        dst = Path(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        sq.save(str(dst), format="PNG")
    return (AVATAR_SIDE, AVATAR_SIDE)


def build_avatar_prompt(entry: dict, style: str) -> str:
    """按底模提示词风格拼头像生成提示词（纯函数）。

    danbooru → tags 串（头像构图词组）；qwen → 中文自然语言短段；
    natural → 英文 caption。角色固有特征照常点名（头像最怕丢耳朵尾巴）。
    """
    name = str(entry.get("name") or "").strip() or "该角色"
    tags = str(entry.get("tags_en") or "").strip().rstrip(",")
    if style == "qwen":
        # v0.42.0：包内运行时必须用相对导入——顶层 `import prompt_gen`
        # 在 AstrBot 插件包里会抛 No module named 'prompt_gen'（用户实报
        # 「生成头像报错」）。本地平铺调试（tests 把插件目录塞进 sys.path）
        # 才回落绝对导入，口径与 webui_api / batch_fill 顶部一致。
        try:
            from . import prompt_gen
        except ImportError:  # pragma: no cover - 本地平铺调试
            import prompt_gen

        segs = [f"一幅{str((entry.get('name') or '')).strip() or '该角色'}的方形头像插画。",
                f"画面主体是{name}：头部与肩部的特写构图，人物面部居中、"
                "占比约七成，视线看向观者。"]
        feats = prompt_gen.identity_feature_text(entry, "zh")
        if feats:
            segs.append(f"角色的{feats}清晰可见、不可省略。")
        if tags:
            segs.append(f"外观参照这些特征：{tags}。")
        segs.append("背景为简洁的浅色纯色，光照柔和通透，整体干净利落、"
                    "细节精致，画面里只有这一个人物。")
        return "。".join(s.strip("。") for s in segs if s.strip()) + "。"
    if style == "natural":
        # v0.42.0：包内运行时必须用相对导入——顶层 `import prompt_gen`
        # 在 AstrBot 插件包里会抛 No module named 'prompt_gen'（用户实报
        # 「生成头像报错」）。本地平铺调试（tests 把插件目录塞进 sys.path）
        # 才回落绝对导入，口径与 webui_api / batch_fill 顶部一致。
        try:
            from . import prompt_gen
        except ImportError:  # pragma: no cover - 本地平铺调试
            import prompt_gen

        segs = ["best quality, highres, solo, portrait, close-up, face focus, "
                "simple background, looking at viewer"]
        if tags:
            segs.append(tags)
        feats = prompt_gen.identity_feature_text(entry, "en")
        if feats:
            segs.append(f"keep {feats} clearly visible")
        return ", ".join(segs)
    # danbooru（默认）：tags 串 + 头像构图词
    base = "1girl, solo, best quality, looking at viewer, portrait, close-up, " \
           "face focus, simple background, upper body"
    return f"{base}, {tags}" if tags else base


# 固定 img2img 编辑指令：有显式生成源的角色走这条（身份锚定最强）。
I2I_INSTRUCTION = (
    "把画面改成该角色的方形头像特写：只保留头部与肩部，人物面部居中放大、"
    "占比约七成，视线看向观者；背景改为简洁的浅色纯色。"
    "保持人物的面部特征、发型发色与角色辨识度不变，只做上述改动。"
)


class AvatarRunner:
    """批量头像生成队列：串行逐角色、单张超时不卡队、可停止（同 batch_fill.BatchRunner）。

    任务 = [entry dict]；只处理还没有头像的角色（调用方筛好）。进程重启即清。
    """

    def __init__(self):
        self.running = False
        self.stop_flag = False
        self.items = []      # [{char_id, name, status, error}]
        self.total = 0
        self.done = 0
        self.ok = 0
        self.failed = 0
        self._task = None

    def snapshot(self) -> dict:
        return {"running": self.running, "total": self.total, "done": self.done,
                "ok": self.ok, "failed": self.failed,
                "stopped": self.stop_flag and not self.running,
                "items": self.items[-80:]}

    def stop(self) -> bool:
        self.stop_flag = True
        return self.running

    def start(self, plugin, entries: list, workflow: str = "") -> int:
        if self.running:
            return -1
        entries = [e for e in (entries or []) if isinstance(e, dict)]
        self.running = True
        self.stop_flag = False
        self.items = [{"char_id": e.get("id"), "name": e.get("name") or "",
                       "status": None, "error": ""} for e in entries]
        self.total = len(entries)
        self.done = self.ok = self.failed = 0
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._run(plugin, entries, workflow))
        return len(entries)

    async def _run(self, plugin, entries: list, workflow: str) -> None:
        # webui_api 顶部不 import 本模块（避免循环依赖），这里延迟引用它的
        # 合成事件与落盘逻辑。
        try:
            # v0.42.0：同 build_avatar_prompt——包内相对导入优先，
            # 否则批量头像也会以 No module named 'webui_api' 整体失败。
            try:
                from . import webui_api
            except ImportError:  # pragma: no cover - 本地平铺调试
                import webui_api

            for i, e in enumerate(entries):
                if self.stop_flag:
                    break
                item = self.items[i] if i < len(self.items) else {}
                try:
                    rel, _w, _h = await webui_api.generate_avatar_for(
                        plugin, e, workflow=workflow)
                    item["status"] = "ok"
                    item["error"] = ""
                    self.ok += 1
                    logger.info(f"[moe_waifu] 批量头像 {e.get('id')} → {rel}")
                except Exception as exc:
                    item["status"] = "failed"
                    item["error"] = str(exc)[:120]
                    self.failed += 1
                    logger.warning(f"[moe_waifu] 批量头像失败 {e.get('id')}：{exc}")
                self.done += 1
        except Exception:
            logger.error("[moe_waifu] 批量头像队列异常退出\n" + traceback.format_exc())
        finally:
            self.running = False


def utc_ts() -> int:
    return int(time.time())
