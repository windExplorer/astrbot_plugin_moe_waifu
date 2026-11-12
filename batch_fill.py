# -*- coding: utf-8 -*-
"""萌萌今日老婆 · 批量补图队列（v0.16.0）。

用户口径：自动伪随机批量生成立绘——全局或按作品/角色选择；前提两条：
① 角色存在可用参考图（strategy.refs / 主参考文件真实存在，base 条目同样要求，
   配置 batch_require_ref 可关——用户明确「不存在则跳过」）；
② 立绘数量不足配置配额（portraits_per_char），缺几张补几张。
每张立绘伪随机套一个内容主题（相邻不重复、多主题轮换避免重复），主题与提示词
随档记录（v0.15.0 的 prompt/theme 列）。队列串行（萌绘侧本就有 _gen_lock），
单张超时由 draw.generate_portrait 的 draw_timeout 兜底——超时/失败记录后继续
下一张，绝不卡整体进度；运行中可停止。

纯函数（theme_plan / pick_candidates / build_tasks）可独立测试；运行时状态是
插件实例内存（BatchRunner），进程重启即清——工坊面板如实提示，不做持久化。
"""
from __future__ import annotations

import asyncio
import hashlib

try:  # AstrBot 运行期
    from astrbot.api import logger
except Exception:  # pragma: no cover - 本地平铺调试
    import logging

    logger = logging.getLogger("moe_waifu")

try:  # 双形态导入（包内正常加载 / 平铺调试）
    from . import draw, pool, prompt_gen
except ImportError:  # pragma: no cover
    import draw  # type: ignore
    import pool  # type: ignore
    import prompt_gen  # type: ignore


# ---------- 纯函数（可独立测试） ----------

def content_theme_ids() -> list:
    """可轮换的内容主题 id（排除 random 与 tpl_ 模板——模板是设定稿底稿，不是场景）。"""
    return [t["id"] for t in prompt_gen.THEMES
            if t["id"] != "random" and not str(t["id"]).startswith("tpl_")]


def theme_plan(char_id: str, salt: str, n: int) -> list:
    """该角色本轮补图的主题序列：伪随机 + 相邻不重复（确定性，可复算）。

    种子源 sha256(salt|batch|char_id)；每张在「排除上一张主题」的列表里按
    派生值挑选——相邻两次必不同，整体分布均匀（避免主题撞车）。返回主题 id
    列表（长度 n；n<=0 → 空表）。
    """
    n = max(0, int(n or 0))
    ids = content_theme_ids()
    if not ids or n <= 0:
        return []
    raw = f"{salt}|batch|{char_id}".encode("utf-8")
    h = int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")
    out = []
    prev = ""
    for i in range(n):
        pool_ids = [t for t in ids if t != prev] or ids
        h = int.from_bytes(
            hashlib.sha256(f"{raw}|{i}".encode("utf-8")).digest()[:8], "big")
        cur = pool_ids[h % len(pool_ids)]
        out.append(cur)
        prev = cur
    return out


def pick_candidates(entries: list, have_count_fn, quota: int,
                    require_ref: bool, has_ref_fn) -> list:
    """按前提筛选补图对象：立绘数不足配额（缺几张补几张，至多 quota - have 张），
    require_ref 开启时再要求角色有可用参考图。返回
    [{entry, have, need}]；不满足前提的不入列（跳过原因由调用方展示）。"""
    quota = max(1, int(quota or 1))
    out = []
    for e in entries or []:
        have = int(have_count_fn(e.get("id") or "") or 0)
        need = quota - have
        if need <= 0:
            continue
        if require_ref and not has_ref_fn(e):
            continue
        out.append({"entry": e, "have": have, "need": min(need, quota)})
    return out


def build_tasks(cands: list, salt: str) -> list:
    """候选 → 具体任务：每角色展开成逐张任务并附主题 id（theme_plan 决定）。"""
    tasks = []
    for c in cands or []:
        entry = c.get("entry") or {}
        themes = theme_plan(str(entry.get("id") or ""), salt, int(c.get("need") or 0))
        for i, theme_id in enumerate(themes):
            tasks.append({"char_id": entry.get("id") or "", "index": i,
                          "theme_id": theme_id,
                          "entry": entry, "have": int(c.get("have") or 0)})
    return tasks


def _synthetic_event():
    """工坊同款合成桩事件（platform=webui）：批量补图不借用聊天会话上下文。

    运行期延迟 import webui_api（webui_api 顶部不 import 本模块，无循环依赖）；
    构造失败返回 None——generate_portrait 对 None event 的行为与 WebUI 试画一致。
    """
    try:
        import webui_api

        return webui_api._synthetic_event()
    except Exception:
        return None


# ---------- 运行时（插件实例内存态） ----------

class BatchRunner:
    """批量补图队列：串行逐张、单张超时不卡队、可停止。

    状态快照 snapshot() 给工坊轮询：running / items（逐张结果）/ 计数。
    进程重启即清（工坊面板如实提示），不做持久化。
    """

    def __init__(self):
        self.running = False
        self.stop_flag = False
        self.items = []      # [{char_id, name, index, theme_id, seq, status, error}]
        self.total = 0
        self._task = None

    def snapshot(self) -> dict:
        done = sum(1 for it in self.items if it.get("status"))
        ok_n = sum(1 for it in self.items if it.get("status") == "ok")
        skip_n = sum(1 for it in self.items if it.get("status") == "skipped")
        fail_n = sum(1 for it in self.items
                     if it.get("status") not in ("ok", "skipped", None))
        return {"running": self.running, "total": self.total, "done": done,
                "ok": ok_n, "skipped": skip_n, "failed": fail_n,
                "stopped": self.stop_flag and not self.running,
                "items": self.items}

    def stop(self) -> bool:
        self.stop_flag = True
        return self.running

    def start(self, plugin, tasks: list) -> int:
        if self.running:
            return -1
        tasks = list(tasks or [])
        self.running = True
        self.stop_flag = False
        self.items = [{"char_id": t.get("char_id"), "name": "",
                       "index": int(t.get("index") or 0),
                       "theme_id": t.get("theme_id") or "", "seq": None,
                       "status": None, "error": ""} for t in tasks]
        self.total = len(tasks)
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._run(plugin, tasks))
        return len(tasks)

    async def _run(self, plugin, tasks: list) -> None:
        try:
            date = plugin._today()
            salt = str(plugin._cfg("salt", "moe-waifu"))
            draw_cfg = plugin._draw_cfg()
            labels = {t["id"]: t["label"] for t in prompt_gen.THEMES}
            for i, task in enumerate(tasks):
                if self.stop_flag:
                    break
                item = self.items[i] if i < len(self.items) else {}
                entry = task.get("entry") or {}
                cid = str(task.get("char_id") or "")
                theme_id = str(task.get("theme_id") or "")
                item["name"] = str(entry.get("name") or cid)
                item["theme_label"] = labels.get(theme_id, theme_id)
                # 每张现取 seq（中途可能有人抽卡/手补，动态取保证不撞号）
                seq = plugin._store.next_portrait_seq(cid) if plugin._store else 0
                item["seq"] = seq
                seed_int = pool.portrait_seed_int(cid, date, seq, salt)
                try:
                    prompt_text, _src = await prompt_gen.build_generation_prompt(
                        plugin.context, draw_cfg, entry, seed_int, theme_id)
                    src, status, err, _dur = await draw.generate_portrait(
                        plugin.context, draw_cfg, entry, seed_int,
                        _synthetic_event(), prompt=prompt_text)
                    if status == "ok" and src:
                        rel = draw.archive_portrait(src, plugin._data_dir,
                                                    cid, date, seq, seed_int)
                        if plugin._store:
                            plugin._store.add_portrait(
                                cid, rel, seed_int, date, seq,
                                entry.get("strategy_type") or "base",
                                prompt=prompt_text, theme=theme_id)
                        item["status"] = "ok"
                        item["error"] = ""
                        item["file"] = rel
                    elif status == "skipped_no_anima":
                        item["status"] = "skipped"
                        item["error"] = "萌绘未就绪"
                        logger.warning("[moe_waifu] 批量补图跳过 %s：萌绘未就绪", cid)
                    else:
                        item["status"] = "failed"
                        item["error"] = str(err or status or "生成失败")[:120]
                        logger.warning("[moe_waifu] 批量补图失败 %s#%s：%s",
                                       cid, seq, item["error"])
                except Exception as e:  # 单张任何异常都不允许卡死队列
                    item["status"] = "failed"
                    item["error"] = str(e)[:120]
                    logger.error("[moe_waifu] 批量补图异常 %s：%s", cid, e,
                                 exc_info=True)
            self.running = False
            logger.info("[moe_waifu] 批量补图结束：共 %d 张", self.total)
        except Exception:
            self.running = False
            logger.error("[moe_waifu] 批量补图队列异常退出", exc_info=True)
