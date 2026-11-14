# -*- coding: utf-8 -*-
"""萌萌今日老婆 · 批量补图队列（v0.16.0）。

用户口径：自动伪随机批量生成立绘——全局或按作品/角色选择；前提两条：
① 角色存在可用参考图（strategy.refs / 主参考文件真实存在，base 条目同样要求，
   配置 batch_require_ref 可关——用户明确「不存在则跳过」）；v0.22.4 起挂了
   图生图源的角色一律按 img2img 出图，不再被条目画法里的 base 带偏；
② 立绘数量不足配置配额（portraits_per_char），缺几张补几张。
每张立绘套一个主题（v0.22.4 起内容:画风 3:2 分层、一轮内不重复、避开该角色
已用主题、续补不重放），主题与提示词随档记录（v0.15.0 的 prompt/theme 列）。
队列串行（萌绘侧本就有 _gen_lock），
单张超时由 draw.generate_portrait 的 draw_timeout 兜底——超时/失败记录后继续
下一张，绝不卡整体进度；运行中可停止。

纯函数（theme_plan / pick_candidates / build_tasks）可独立测试；运行时状态是
插件实例内存（BatchRunner），进程重启即清——工坊面板如实提示，不做持久化。
"""
from __future__ import annotations

import asyncio
import hashlib
import json

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
    """可轮换的内容主题 id（排除 random 与 tpl_ 模板——模板是设定稿底稿，不是场景）。

    v0.19.0：`art_*` 纯画风主题（只约束画风）也在此列——批量补图时「这一批换成
    水彩/水墨/赛博」是用户要的效果，不该只能一张张手点。
    """
    return [t["id"] for t in prompt_gen.THEMES
            if t["id"] != "random" and not str(t["id"]).startswith("tpl_")]


def theme_plan(char_id: str, salt: str, n: int, have: int = 0,
               date: str = "", used: list | None = None) -> list:
    """该角色本轮补图的主题序列（v0.22.4 重做）。

    旧版三个毛病对应「一个角色这些主题、另一个角色也是这些」：
    ① 轮换池 32 个里 22 个是 art_* 纯画风主题，等权抽 → 每角色 5 张里
      平均 3~4 张都是「画风·X」，只换画风不换场景，观感高度同质；
    ② 只保证「相邻不重复」，一轮 5 张里仍有约 17% 出现隔位重复主题；
    ③ 种子只有 salt|batch|char_id → 二次补图永远重放开头两个主题。
    现口径：内容:画风 = 3:2 交错，两池各自按种子洗牌（决定论）；优先排除
    该角色已用过的主题，一轮内不重复；种子含 have/date，续补不重放。
    """
    n = max(0, int(n or 0))
    ids = content_theme_ids()
    if not ids or n <= 0:
        return []
    content = [t for t in ids if not str(t).startswith("art_")]
    art = [t for t in ids if str(t).startswith("art_")]
    # v0.25.7：画风主题按现代化程度加权入池（现代系 3~4、中性 2、复古工艺 1），
    # 洗牌时现代画风被抽中的概率明显高于复古（用户反馈「大部分太复古」）。
    art_pool: list = []
    for t in art:
        art_pool.extend([t] * int(prompt_gen.art_theme_weight(t)))
    base = f"{salt}|batch|{char_id}|{date}|{int(have or 0)}"

    def shuffled(lst: list, tag: str) -> list:
        out = list(lst)
        for i in range(len(out) - 1, 0, -1):
            raw = f"{base}|{tag}|{i}".encode("utf-8")
            j = int.from_bytes(hashlib.sha256(raw).digest()[:8], "big") % (i + 1)
            out[i], out[j] = out[j], out[i]
        return out

    used_set = {str(x) for x in (used or []) if str(x).strip()}
    out: list = []
    prev = ""
    round_no = 0
    while len(out) < n and round_no < 64:
        # 第一轮只用没用过的主题；用尽后放宽，保证 n 张凑得齐
        cpool = shuffled(content, f"c{round_no}")
        apool = shuffled(art_pool, f"a{round_no}")
        if round_no == 0:
            cpool = [t for t in cpool if t not in used_set]
            apool = [t for t in apool if t not in used_set]
        merged: list = []
        ci = ai = 0
        while ci < len(cpool) or ai < len(apool):
            for _ in range(3):
                if ci < len(cpool):
                    merged.append(cpool[ci]); ci += 1
            for _ in range(2):
                if ai < len(apool):
                    merged.append(apool[ai]); ai += 1
        for t in merged:
            if t == prev or t in out:   # 一轮内不重复（顺带保证相邻不重复）
                continue
            out.append(t)
            prev = t
            if len(out) >= n:
                break
        round_no += 1
    # 池子总量小于 n（配额 > 32）时按序复用补齐，仍避开相邻重复
    if len(out) < n:
        fallback = content + art
        k = 0
        while len(out) < n and k < n * 4:
            t = fallback[k % len(fallback)]
            if t != prev:
                out.append(t)
                prev = t
            k += 1
    return out[:n]


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


def char_salt(salt: str, char_id: str, nonce: int = 0) -> str:
    """单角色「换一个」用的盐：nonce>0 时派生独立盐。

    预览重随机与真正启动必须走同一套盐——否则界面显示换过了，实跑又按
    原种子重放一遍。nonce=0（默认）返回原盐，行为与之前完全一致。
    """
    n = int(nonce or 0)
    if n <= 0:
        return str(salt)
    raw = f"{salt}|reroll|{char_id}|{n}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def build_char_tasks(entry: dict, themes: list, have: int = 0) -> list:
    """单角色补齐（v0.32.2）：显式主题序列 → 逐张任务（与 build_tasks 输出同形）。

    详情页「补齐配额」的预览弹窗允许逐张换随机 / 整体重随机——实跑必须按
    用户确认过的序列出图（预览 = 实出），所以任务直接吃显式列表，不再走
    theme_plan。调用方负责校验主题 id 合法、张数与配额差一致。
    """
    cid = str((entry or {}).get("id") or "")
    out = []
    for i, tid in enumerate(themes or []):
        out.append({"char_id": cid, "index": i, "theme_id": str(tid or ""),
                    "entry": entry, "have": int(have or 0)})
    return out


def build_tasks(cands: list, salt: str, date: str = "",
                overrides: dict | None = None) -> list:
    """候选 → 具体任务：每角色展开成逐张任务并附主题 id（theme_plan 决定）。

    v0.22.4：have / date / used_themes 一并交给 theme_plan；预览与实际启动
    必须传同样的 date，否则「预览写着校园、实出泳装」。
    v0.28.3：overrides = {char_id: nonce}，预览里点过「换一个」的角色按
    派生盐重算主题序列（见 char_salt）。
    """
    ov = overrides if isinstance(overrides, dict) else {}
    tasks = []
    for c in cands or []:
        entry = c.get("entry") or {}
        cid = str(entry.get("id") or "")
        themes = theme_plan(
            cid, char_salt(salt, cid, ov.get(cid, 0)), int(c.get("need") or 0),
            have=int(c.get("have") or 0), date=date,
            used=c.get("used_themes") or [])
        for i, theme_id in enumerate(themes):
            tasks.append({"char_id": entry.get("id") or "", "index": i,
                          "theme_id": theme_id,
                          "entry": entry, "have": int(c.get("have") or 0)})
    return tasks


def effective_batch_entry(entry: dict, data_dir=None) -> dict:
    """批量补图用条目：挂了可用图生图源就强制按 img2img 出图（v0.22.4）。

    根因：入列前提 has_gen_refs() 只看「有没有 ref/gen_refs 文件」，不看
    strategy_type；工坊把画法切回 base 时旧 ref 会刻意保留（merge_saved_entry），
    于是这些角色进了批量队列，generate_portrait 却按 base 走文生图——
    参考图白挂、输出还是 base 直出（还顺带踩中随机多视图）。
    批量补图的语义就是「按参考图补立绘」，这里统一纠正画法；effective_entry
    只改 type，原 ref / gen_refs / denoise 全部沿用。没有图生图源的角色保持
    原画法（batch_require_ref=false 时的 base/lora 直出）。纯函数，可独立测试。
    """
    if str(entry.get("strategy_type") or "base") == "img2img":
        return entry
    if not draw.has_gen_refs(entry, data_dir):
        return entry
    return draw.effective_entry(entry, {"strategy_type": "img2img"})


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
                cid = str(task.get("char_id") or "")
                theme_id = str(task.get("theme_id") or "")
                # v0.22.4：挂了参考图就按图生图出图（有效条目同时喂给提示词
                # 与出图，避免「提示词按文生图 tag 拼、出图按 img2img 编辑」）。
                entry = effective_batch_entry(
                    task.get("entry") or {}, plugin._data_dir)
                item["name"] = str(entry.get("name") or cid)
                item["theme_label"] = labels.get(theme_id, theme_id)
                # 每张现取 seq（中途可能有人抽卡/手补，动态取保证不撞号）
                seq = plugin._store.alloc_portrait_seq(cid) if plugin._store else 0
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
                            # v0.20.0：变化维度 / 图片尺寸随档记录
                            _w, _h = draw.image_size(plugin._data_dir / rel)
                            plugin._store.add_portrait(
                                cid, rel, seed_int, date, seq,
                                entry.get("strategy_type") or "base",
                                prompt=prompt_text, theme=theme_id,
                                dims=json.dumps(prompt_gen.describe_variations(
                                    seed_int, theme_id,
                                    force_single=(str(entry.get("strategy_type") or "")
                                                  == "img2img"),
                                    keep=prompt_gen.keep_dims_of(entry)),
                                    ensure_ascii=False),
                                width=_w, height=_h,
                                duration_ms=_dur)
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
