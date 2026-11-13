# -*- coding: utf-8 -*-
"""萌萌今日老婆：二次元角色每日抽卡插件入口。

流程（PRD §2/§5.2）：/今日老婆 → 种子决定角色 → 立绘库决策（当日复用 / 生成存档 /
满库取图）→ Pillow 出卡落库 → 发卡。**硬依赖萌绘**：需要生图而萌绘不可用 →
报错不出卡（不做无图降级）；满库角色不受影响。
"""
import asyncio
import importlib
import json
import random
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api import logger
from astrbot.api.message_components import Image, Plain
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.message_event_result import MessageChain

from . import card, draw, pool, prompt_gen, store

PLUGIN_NAME = "astrbot_plugin_moe_waifu"

# 卡图保留天数（只清渲染产物，不动 draws 行与立绘库）
_CARD_KEEP_DAYS = 90
# 试画缓存保留天数（工坊试画成品拷贝，供大图查看；过期回收）
_TRY_CACHE_KEEP_DAYS = 7

# 抽卡过程提示（v0.19.9：全部改口语化说法——v0.19.8 前混着「进行中，完成后会发送」
# 这类系统工单腔，发到群里很出戏；萌系 bot 的等待提示也要说人话）。
# 萌绘单张 20~120s，图生图更慢；只说明「正在抽、大概要等」，不额外加戏。
_SUMMON_LINES = [
    "正在为你抽今日角色，请稍候……",
    "抽卡中，正在确认今日这一签，请稍等。",
    "正在抽取今日角色，马上就好。",
    "签已经抽好了，画完马上发你～",
    "今日角色抽取中，稍后即可查看。",
    "正在准备今日的抽卡结果，请稍候。",
]
# 图生图模板模式的抽卡提示（图生图更慢，顺带交代原因）
_I2I_LINES = [
    "对照参考图来画，比平时慢一点，请稍候……",
    "今日这一签要照着参考图画，稍等片刻～",
    "正在照着参考图把她画出来，画好马上发给你～",
]
# 排队提示（多人同时抽卡时告知前面还有几个请求）
_QUEUE_LINES = [
    "前面还有 {n} 位在排队抽卡，请稍等～",
    "前面还有 {n} 位小伙伴在排队，轮到你马上就发～",
]


def _plugin_version() -> str:
    """@register 的版本取自 metadata.yaml（唯一版本来源，星语同款）。"""
    try:
        text = (Path(__file__).resolve().parent / "metadata.yaml").read_text(encoding="utf-8")
        m = re.search(r"(?m)^\s*version:\s*v?([0-9][^\s#]*)", text)
        return m.group(1).strip() if m else "0.0.0"
    except Exception:
        return "0.0.0"


@register(PLUGIN_NAME, "windExplorer", "萌萌今日老婆：每日二次元角色抽卡", _plugin_version())
class MoeWaifuPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        # ⚠️ 必须接受 config kwarg：star_manager 注入失败会被静默降级，
        # self.config 缺失导致所有配置回落默认值（星语踩过的坑）。
        super().__init__(context)
        self.config = config
        self._store = None
        self._data_dir = None
        # 同一 (uid,date) 只允许一条召唤流程在飞：防连点触发重复生图/重复计数
        self._inflight: set = set()
        # 生图串行队列（v0.9.6）：萌绘一张 20~120s，多召唤并发时逐张画防打爆后端；
        # _gen_queue 是正在排队/作画的任务数，用于「前面还有 N 位」友好提示
        self._gen_lock = asyncio.Lock()
        self._gen_queue = 0
        # 工坊后台试画/补图 job（webui_api 用）：job_id -> dict
        self._jobs: dict = {}
        # 批量补图队列（v0.16.0）：进程内存态，重启即清
        self._batch = None
        self._reload_modules()

    # ---------- 生命周期 ----------

    def _reload_modules(self):
        """热重载自研子模块（依赖序）。main 里全部走模块属性访问（pool.xxx），
        reload 后自动生效，无需重新绑定函数对象。"""
        pkg = __package__
        for name in ("store", "card", "pool", "draw", "prompt_gen",
                     "batch_fill", "webui_api"):
            mod = sys.modules.get(f"{pkg}.{name}")
            if mod is not None:
                try:
                    importlib.reload(mod)
                    logger.info(f"[{PLUGIN_NAME}] 已热重载模块 {name}")
                except Exception:
                    logger.error(f"[{PLUGIN_NAME}] 热重载 {name} 失败\n{traceback.format_exc()}")

    async def initialize(self):
        try:
            self._data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
            bundled = Path(__file__).resolve().parent / "seed_pool.json"
            pool.install_seed_pool(self._data_dir / "pools", bundled, logger)
            self._store = store.Store(self._data_dir / "waifu.db")
            # v0.24.0 关系模型阶段 1：幂等迁移（只纳入三游戏）。表已由
            # Store.__init__ 建好；地基失败不能阻塞插件启动。
            try:
                self._store.migrate_relational(
                    self._data_dir / "pools", self._data_dir, logger=logger)
            except Exception:
                logger.error(f"[{PLUGIN_NAME}] 关系模型迁移失败\n"
                             f"{traceback.format_exc()}")
            # v0.25.0：读路径切到新表（空表 / 异常时 pool 内部自动回退 JSON）
            try:
                pool.set_packs_provider(self._store.load_packs)
            except Exception:
                logger.error(f"[{PLUGIN_NAME}] 读路径切换新表失败\n"
                             f"{traceback.format_exc()}")
            try:  # 角色库工坊（Dashboard 登录墙内，仅管理员使用）
                from . import webui_api
                webui_api.register(self)
            except Exception:
                logger.error(f"[{PLUGIN_NAME}] 工坊 WebUI 注册失败\n{traceback.format_exc()}")
            try:  # 清理过期卡图（只删渲染产物，不动 draws 行与立绘库）
                removed = await asyncio.to_thread(self._prune_cards, self._data_dir)
                if removed:
                    logger.info(f"[{PLUGIN_NAME}] 已清理 {removed} 张过期卡图")
            except Exception:
                logger.warning(f"[{PLUGIN_NAME}] 卡图清理失败（不影响出卡）\n{traceback.format_exc()}")
            try:  # 清理过期试画缓存（7 天）
                removed = await asyncio.to_thread(self._prune_try_cache, self._data_dir)
                if removed:
                    logger.info(f"[{PLUGIN_NAME}] 已清理 {removed} 张过期试画缓存")
            except Exception:
                logger.warning(f"[{PLUGIN_NAME}] 试画缓存清理失败（忽略）\n{traceback.format_exc()}")
            logger.info(f"[{PLUGIN_NAME}] 初始化完成，数据目录：{self._data_dir}")
        except Exception:
            logger.error(f"[{PLUGIN_NAME}] 初始化失败\n{traceback.format_exc()}")
            raise

    async def terminate(self):
        if self._store is not None:
            self._store.close()
            self._store = None

    @staticmethod
    def _prune_cards(data_dir: Path) -> int:
        cards_dir = Path(data_dir) / "cards"
        if not cards_dir.is_dir():
            return 0
        deadline = time.time() - _CARD_KEEP_DAYS * 86400
        removed = 0
        for f in cards_dir.glob("*.png"):
            try:
                if f.stat().st_mtime < deadline:
                    f.unlink()
                    removed += 1
            except Exception:
                pass
        return removed

    @staticmethod
    def _prune_try_cache(data_dir: Path) -> int:
        cache_dir = Path(data_dir) / "try_cache"
        if not cache_dir.is_dir():
            return 0
        deadline = time.time() - _TRY_CACHE_KEEP_DAYS * 86400
        removed = 0
        for f in cache_dir.glob("*.png"):
            try:
                if f.stat().st_mtime < deadline:
                    f.unlink()
                    removed += 1
            except Exception:
                pass
        return removed

    # ---------- 工具 ----------

    def _cfg(self, key: str, default):
        try:
            value = self.config.get(key, default)
            return default if value is None else value
        except Exception:
            return default

    def _today(self) -> str:
        try:
            tz = ZoneInfo(str(self._cfg("timezone", "Asia/Shanghai")))
        except Exception:
            tz = None
        return datetime.now(tz).strftime("%Y-%m-%d") if tz else datetime.now().strftime("%Y-%m-%d")

    def _disabled_groups(self) -> set:
        raw = str(self._cfg("disabled_groups", "") or "")
        return {line.strip() for line in raw.splitlines() if line.strip()}

    def _draw_cfg(self) -> dict:
        return {
            "draw_width": self._cfg("draw_width", 700),
            "draw_height": self._cfg("draw_height", 1200),
            "draw_timeout": self._cfg("draw_timeout", 120),
            "draw_workflow": self._cfg("draw_workflow", ""),
            "draw_negative_enabled": self._cfg("draw_negative_enabled", True),
            "draw_negative_prompt": self._cfg("draw_negative_prompt", ""),
            "img2img_workflow": self._cfg("img2img_workflow", ""),
            "img2img_denoise": self._cfg("img2img_denoise", 0.65),
            "prompt_llm_enabled": self._cfg("prompt_llm_enabled", True),
            "prompt_llm_provider": self._cfg("prompt_llm_provider", ""),
            "prompt_llm_timeout": self._cfg("prompt_llm_timeout", 30),
            "variations_enabled": self._cfg("variations_enabled", True),
            # 参考图里相对路径（refs/xxx.png）的解析基准：draw 侧按 CWD 找不到
            "data_dir": str(self._data_dir) if self._data_dir else "",
        }

    def _load_packs(self) -> list:
        return pool.load_packs(self._data_dir / "pools", logger)

    @staticmethod
    async def _send_plain(event: AstrMessageEvent, text: str) -> None:
        """直发纯文本，绕开结果链装饰（不会被自动 @ 发送者；星语同款）。"""
        try:
            await event.send(MessageChain([Plain(str(text))]))
        except Exception:
            logger.warning(f"[{PLUGIN_NAME}] 过程提示直发失败（忽略）\n{traceback.format_exc()}")

    def _draw_error_text(self, status: str, err: str) -> str:
        """发给普通用户的抽卡失败提示：只说失败/稍后再试，不带技术细节。

        萌绘是否安装、超时秒数、异常文本、配置路径这些只写 AstrBot 日志，
        管理员在后台日志/工坊里排查；群里发这些对普通用户没有意义
        （用户口径 v0.12.3）。
        """
        if status == "skipped_no_anima":
            logger.warning(
                f"[{PLUGIN_NAME}] 抽卡失败：萌绘不可用（comfyui_draw/comfyui_img2img "
                "工具不存在），请安装并配置 astrbot_plugin_comfyui_anima（v7.7.58+）")
            return "抽卡失败，请稍后再试。"
        if status == "timeout":
            logger.warning(
                f"[{PLUGIN_NAME}] 抽卡失败：出图超时（>{int(self._cfg('draw_timeout', 120))}s），"
                "萌绘可能正忙或工作流过重")
            return "抽卡超时，请稍后再试。"
        logger.warning(f"[{PLUGIN_NAME}] 抽卡失败：{err[:300]}")
        return "抽卡失败，请稍后再试。"

    @staticmethod
    def _summon_line(decision: str, queued: int) -> str:
        """召唤过程提示（随机、非平白直述）：排队时告知前面还有几位在画；
        否则按画法随机一句（图生图模板有专属台词）。"""
        if queued > 0:
            return random.choice(_QUEUE_LINES).format(n=queued)
        lines = _I2I_LINES if decision == "generate_i2i" else _SUMMON_LINES
        return random.choice(lines)

    # ---------- 指令 ----------

    @filter.command("今日老婆", alias={"老婆", "jrlp"})
    async def waifu_cmd(self, event: AstrMessageEvent):
        """抽今日老婆：一人一天一位，种子决定，萌绘立绘卡片。"""
        if self._store is None or self._data_dir is None:
            yield event.plain_result("插件尚未初始化完成，请稍后再试。")
            return
        uid = str(event.get_sender_id() or "").strip()
        if not uid:
            yield event.plain_result("无法识别你的账号，请稍后再试。")
            return
        gid = "" if event.is_private_chat() else str(event.get_group_id() or "")
        if gid and gid in self._disabled_groups():
            yield event.plain_result("本群暂未开启抽卡。")
            return
        date = self._today()

        existing = self._store.get_draw(uid, date)
        if existing:
            card_path = str(existing.get("card_path") or "")
            if card_path and Path(card_path).is_file():
                yield event.chain_result([
                    Image.fromFileSystem(card_path),
                    Plain("这是你今天抽到的角色，明天可以再抽一位。"),
                ])
                return
            # 卡图被清理过：立绘库里还有底图就重渲染一张（不重新生图）
            entry = pool.find_entry(self._load_packs(), existing["char_id"])
            rel = self._store.portrait_by_date(existing["char_id"], date) \
                or (self._store.list_portraits(existing["char_id"]) or [None])[-1]
            if entry is None:
                yield event.plain_result("今天已经抽过了，但这位角色已不在角色库中，明天再来吧。")
                return
            if rel is not None and (self._data_dir / rel["file"]).is_file():
                # 印象语按签随机选句：优先用落库的种子快照（换老婆后句子跟着变）
                seed_hex = str(existing.get("seed_hex") or "")
                if len(seed_hex) == 64:
                    try:
                        seed_bytes = bytes.fromhex(seed_hex)
                    except ValueError:
                        seed_bytes = pool.derive_seed(
                            date, uid, str(self._cfg("salt", "moe-waifu")),
                            int(existing.get("nonce") or 0), self._store.get_epoch(uid))
                else:
                    seed_bytes = pool.derive_seed(
                        date, uid, str(self._cfg("salt", "moe-waifu")),
                        int(existing.get("nonce") or 0), self._store.get_epoch(uid))
                entry = dict(entry)
                entry["quote"] = pool.pick_quote(entry, seed_bytes)
                try:
                    card_path = await asyncio.to_thread(
                        card.render_card, self._data_dir / rel["file"], entry,
                        date_str=date, same_count=1, total_count=1,
                        rerolled=bool(existing["rerolled"]),
                        out_path=str(self._data_dir / "cards" / f"{uid}_{date}.png"),
                        data_dir=self._data_dir,
                    )
                except Exception:
                    logger.error(f"[{PLUGIN_NAME}] 卡图重渲染失败\n{traceback.format_exc()}")
                    yield event.plain_result("抽卡失败，请稍后再试。")
                    return
                self._store.update_draw_card(uid, date, card_path)
                yield event.chain_result([Image.fromFileSystem(card_path)])
                return
            # 没有可用底图（立绘库被清 / 调试「强制换签」已重派新角色）：
            # 按当前 nonce 走完整产出流程重出（出图/取图 → 落库 → 渲染）
            key = (uid, date)
            if key in self._inflight:
                yield event.plain_result("正在抽卡中，请稍后再查看。")
                return
            self._inflight.add(key)
            try:
                card_path = await self._produce(
                    event, entry, uid, date,
                    nonce=int(existing.get("nonce") or 0),
                    rerolled=bool(existing["rerolled"]),
                )
            finally:
                self._inflight.discard(key)
            if card_path:
                yield event.chain_result([Image.fromFileSystem(card_path)])
            return

        # 首抽
        packs = self._load_packs()
        candidates = pool.candidates_for(
            packs, gid, str(self._cfg("group_pools", "") or ""),
            bool(self._cfg("allow_unverified", False)),
        )
        if not candidates:
            logger.warning(
                f"[{PLUGIN_NAME}] 抽卡失败：没有可抽角色"
                "（pools/ 下没有启用的角色卷，或卷内角色均未 verified）")
            yield event.plain_result("抽卡失败，暂时没有可抽的角色，请稍后再试。")
            return
        salt = str(self._cfg("salt", "moe-waifu"))
        entry = pool.pick_character(
            pool.derive_seed(date, uid, salt, 0, self._store.get_epoch(uid)), candidates)
        key = (uid, date)
        if key in self._inflight:
            yield event.plain_result("正在抽卡中，请稍后再查看。")
            return
        self._inflight.add(key)
        try:
            card_path = await self._produce(event, entry, uid, date, nonce=0, rerolled=False)
        finally:
            self._inflight.discard(key)
        if card_path:
            yield event.chain_result([Image.fromFileSystem(card_path)])

    @filter.command("换老婆", alias={"换老婆卡"})
    async def reroll_cmd(self, event: AstrMessageEvent):
        """重抽今日老婆：nonce+1 得新角色，受每日次数限制。"""
        if self._store is None or self._data_dir is None:
            yield event.plain_result("插件尚未初始化完成，请稍后再试。")
            return
        uid = str(event.get_sender_id() or "").strip()
        if not uid:
            yield event.plain_result("无法识别你的账号，请稍后再试。")
            return
        gid = "" if event.is_private_chat() else str(event.get_group_id() or "")
        date = self._today()
        limit = int(self._cfg("reroll_daily_limit", 1))
        if limit <= 0:
            yield event.plain_result("今日重抽功能未开启。")
            return
        existing = self._store.get_draw(uid, date)
        if not existing:
            yield event.plain_result("今天还没抽过，先发送 /今日老婆。")
            return
        used = int(existing.get("nonce") or 0)
        if used >= limit:
            yield event.plain_result(f"今日重抽次数已用完（{limit} 次），明天再来吧。")
            return
        packs = self._load_packs()
        candidates = pool.candidates_for(
            packs, gid, str(self._cfg("group_pools", "") or ""),
            bool(self._cfg("allow_unverified", False)),
        )
        if not candidates:
            logger.warning(f"[{PLUGIN_NAME}] 重抽失败：没有可抽角色")
            yield event.plain_result("抽卡失败，暂时没有可抽的角色。")
            return
        salt = str(self._cfg("salt", "moe-waifu"))
        entry = pool.pick_character(
            pool.derive_seed(date, uid, salt, used + 1, self._store.get_epoch(uid)),
            candidates)
        key = (uid, date)
        if key in self._inflight:
            yield event.plain_result("正在抽卡中，请稍后再查看。")
            return
        self._inflight.add(key)
        try:
            card_path = await self._produce(event, entry, uid, date, nonce=used + 1, rerolled=True)
        finally:
            self._inflight.discard(key)
        if card_path:
            yield event.chain_result([Image.fromFileSystem(card_path)])

    @filter.command("老婆帮助", alias={"waifu帮助"})
    async def help_cmd(self, event: AstrMessageEvent):
        # v0.17.0：帮助图（纯 Pillow，复用 card.py 视觉语言）；渲染失败回落纯文本
        try:
            path = card.render_help(version=f"v{_plugin_version()}",
                                    data_dir=str(self._data_dir))
            yield event.chain_result([Image.fromFileSystem(path)])
            return
        except Exception:
            logger.error("[{}] 帮助图渲染失败，回落纯文本".format(PLUGIN_NAME),
                         exc_info=True)
        yield event.plain_result(
            "萌萌今日老婆\n"
            "/今日老婆（别名 /老婆）：抽取今天的角色，一人一天一位\n"
            "/换老婆：重新抽一位（受「每日换老婆次数」限制）\n\n"
            "抽到的角色会附上一张卡图；同一角色的卡图会逐步积累，之后可能直接复用。\n"
            "角色库、试画、立绘维护等管理功能在 AstrBot 后台的插件页里，仅管理员可见。"
        )

    # ---------- 核心产出 ----------

    async def _produce(self, event: AstrMessageEvent, entry: dict, uid: str, date: str,
                       nonce: int, rerolled: bool) -> str | None:
        """立绘库决策 → 出图/取图 → 计数落库 → 渲染出卡。失败返回 None（已发错误提示）。"""
        gid = "" if event.is_private_chat() else str(event.get_group_id() or "")
        gname = ""
        try:
            gname = str(event.get_group_name() or "") if gid else ""
        except Exception:
            pass
        nickname = ""
        try:
            nickname = str(event.get_sender_name() or "")
        except Exception:
            pass
        platform = str(getattr(event, "platform", "") or "")

        char_id = entry["id"]
        salt = str(self._cfg("salt", "moe-waifu"))
        quota = max(1, int(self._cfg("portraits_per_char", 5)))
        today_row = self._store.portrait_by_date(char_id, date)
        pool_size = self._store.count_portraits(char_id)
        # v0.9.6：种子先派生（重置轮次 epoch 一并计入），出图方式按种子规划——
        # 同一签恒定（ mix 模式下「取库存还是图生图」不随重试漂移）
        seed_bytes = pool.derive_seed(date, uid, salt, nonce, self._store.get_epoch(uid))
        mode = str(self._cfg("gen_source_mode", "mix") or "mix")
        i2i_prob = self._cfg("img2img_prob", 50)
        try:
            i2i_prob = int(i2i_prob)
        except (TypeError, ValueError):
            i2i_prob = 50
        # 参考图是否可用必须按「文件真实存在」判定（v0.9.9）：工坊会把
        # `refs/xxx.png` 这类相对 data 目录的路径写进 refs，只查字符串非空会
        # 让「没绑图生图源」的签被规划成 generate_i2i，先发图生图台词、
        # 再在 generate_portrait 里找不到参考图直接抛异常。
        data_dir = str(self._data_dir) if self._data_dir else ""
        # v0.10.0：出卡图生图源是**独立且必须显式指定**的 strategy.draw_ref，
        # 与「参考图列表」strategy.refs 无关——没指定就不规划图生图、
        # 不播图生图台词，更不会从列表里私自挑一张。
        draw_src = draw.draw_source(entry, data_dir)
        # v0.19.3：mode 一并传入——img2img（只用图生图模板）不能被「满配额取库」
        # 短路，否则库满后永远取旧立绘（用户实报）
        # v0.19.4：roll 提出来复用（决策 + 事后解释同源）
        roll = pool.sub_int(seed_bytes, "genmode", 100)
        decision = draw.decision_of(char_id, date, today_row, pool_size, quota, mode)
        if decision == "generate":
            decision = draw.plan_source(mode, pool_size, quota,
                                        bool(draw_src), roll, i2i_prob)
        # 二次确认（文件可能在规划后被清理）
        i2i_degraded = False
        if decision == "generate_i2i" and not draw_src:
            decision = "generate"
            i2i_degraded = True
        if decision == "generate_i2i":
            # 把指定的出卡源注入 ref：generate_portrait 只认显式 ref，
            # 不退回参考图库随机取图；条目本身是 img2img 也强制用这一张。
            # effective_entry 会按 override 重建 img2img 字段，条目级图生图
            # 工作流要一并带上，否则抽卡会静默丢掉用户选的工作流。
            # refs 显式只放这一张：出卡永远只认 draw_ref，不混入条目的多图源
            override = {"strategy_type": "img2img", "ref": draw_src, "refs": [draw_src]}
            st_wf = str((entry.get("strategy") or {}).get("img2img_workflow")
                        or "").strip()
            if st_wf:
                override["img2img_workflow"] = st_wf
            entry = draw.effective_entry(entry, override)

        src = None
        rel_path = ""
        pool_missing = False
        if decision == "reuse_today":
            src = self._data_dir / today_row["file"]
            rel_path = str(today_row["file"])
        elif decision == "from_pool":
            idx = pool.pool_index(char_id, date, pool_size, salt)
            rows = self._store.list_portraits(char_id)
            row = rows[idx % len(rows)]
            rel_path = str(row["file"])
            src = self._data_dir / rel_path
            if not src.is_file():  # 库文件被手动删了：回退为重新生成
                src = None
                decision = "generate"  # 记录真实发生的决策，别把它记成「库存复用」
                pool_missing = True

        if src is None:  # generate / generate_i2i
            # v0.15.0：seq 改「历史最大 + 1」——旧实现 seq = 库存数，删过立绘后
            # count 回退，下一次生成复用旧 seq → 编号重复、同日种子撞车。
            # pool_size 只留给上方配额判定，不再兼做序号。
            seq = self._store.alloc_portrait_seq(char_id)
            seed_int = pool.portrait_seed_int(char_id, date, seq, salt)
            if bool(self._cfg("announce_enabled", True)):
                await self._send_plain(event, self._summon_line(decision, self._gen_queue))
            # 提示词先产（v0.5.6）：img2img 由 LLM 生成 Qwen-Image-Edit 编辑指令
            # （失败自动回落本地模板），base/lora 为 tags + 变化维度
            draw_cfg = self._draw_cfg()
            prompt_text, prompt_src = await prompt_gen.build_generation_prompt(
                self.context, draw_cfg, entry, seed_int
            )
            logger.info(f"[{PLUGIN_NAME}] 立绘提示词（{prompt_src}）：{prompt_text[:80]}")
            # 生图串行队列：萌绘一张 20~120s，多召唤并发时逐张画（提示词生成
            # 不占槽——那是本地/LLM 的活，不该堵住后面人的提示词）
            self._gen_queue += 1
            try:
                async with self._gen_lock:
                    raw_src, status, err, dur = await draw.generate_portrait(
                        self.context, draw_cfg, entry, seed_int, event, prompt=prompt_text
                    )
            finally:
                self._gen_queue -= 1
            self._store.log_job(char_id, date, "generated", status, uid,
                                entry["strategy_type"], seed_int, err, dur, raw_src or "")
            if raw_src is None:
                await self._send_plain(event, self._draw_error_text(status, err))
                return None
            rel_path = draw.archive_portrait(raw_src, self._data_dir, char_id, date, seq, seed_int)
            # v0.15.0：随档记录提示词；抽卡主链路不套主题（维度纯随机），theme 恒空
            # v0.20.0：变化维度 / 图片尺寸随档记录（详情弹窗查看）
            _w, _h = draw.image_size(self._data_dir / rel_path)
            self._store.add_portrait(
                char_id, rel_path, seed_int, date, seq,
                entry["strategy_type"], prompt=prompt_text, theme="",
                dims=json.dumps(prompt_gen.describe_variations(
                    seed_int, force_single=(entry["strategy_type"] == "img2img")),
                    ensure_ascii=False),
                width=_w, height=_h,
                duration_ms=dur)
            src = self._data_dir / rel_path

        # 落库与计数（先 upsert 再查同款，含自己）
        # v0.9.0：角色名/作品/种子/立绘来源随签快照落库，历史记录不再依赖角色库反查；
        # upsert_draw 同时追加一条只写不改的抽签流水，换老婆前的角色也留得住。
        # v0.9.5：种子带重置轮次 epoch；卡片印象语按签从 quotes 池随机选一句
        entry = dict(entry)
        entry["quote"] = pool.pick_quote(entry, seed_bytes)
        # v0.19.4：决策原因随签落库 + 进日志。用户口径：「每次抽卡用的什么方式、
        # 为什么、什么原因导致回退」要能事后查到——光有 decision 一个词不够。
        note = draw.explain_decision(mode, decision, pool_size, quota,
                                     bool(draw_src), i2i_prob, roll,
                                     today_portrait=bool(today_row))
        if pool_missing:
            note += "；该库存立绘文件已丢失，本次改为重新生成"
        if i2i_degraded:
            note += "；出卡源在规划后不可用，降级为按角色画法生成"
        logger.info(
            "[" + PLUGIN_NAME + "] 抽卡决策 "
            + f"{entry.get('name') or char_id}（{char_id}）uid={uid} "
            + f"date={date} nonce={nonce} → {decision}｜{note}"
        )
        self._store.upsert_draw(
            uid, date, char_id, nonce, rerolled, nickname, gid, gname, platform,
            seed_hex=seed_bytes.hex(),
            char_name=str(entry.get("name") or ""),
            series=str(entry.get("series") or ""),
            portrait_file=rel_path,
            decision=decision,
            decision_note=note,
        )
        total = self._store.bump_user_char(uid, char_id)
        self._store.bump_char_stats(char_id, date)
        same = self._store.same_group_today(gid, date, char_id)

        cards_dir = self._data_dir / "cards"
        cards_dir.mkdir(parents=True, exist_ok=True)
        try:
            card_path = await asyncio.to_thread(
                card.render_card, src, entry,
                date_str=date, same_count=same, total_count=total, rerolled=rerolled,
                out_path=str(cards_dir / f"{uid}_{date}.png"), data_dir=self._data_dir,
            )
        except Exception:
            # 渲染异常只写日志：群里只回一句通用失败提示，不把栈信息抛给用户
            logger.error(f"[{PLUGIN_NAME}] 卡图渲染失败\n{traceback.format_exc()}")
            await self._send_plain(event, "抽卡失败，请稍后再试。")
            return None
        self._store.update_draw_card(uid, date, card_path)
        if decision != "generate":
            self._store.log_job(char_id, date, decision, "from_pool", uid,
                                entry["strategy_type"], None, "", 0, str(src))
        return card_path
