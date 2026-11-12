# -*- coding: utf-8 -*-
"""萌萌今日老婆：二次元角色每日抽卡插件入口。

流程（PRD §2/§5.2）：/今日老婆 → 种子决定角色 → 立绘库决策（当日复用 / 生成存档 /
满库取图）→ Pillow 出卡落库 → 发卡。**硬依赖萌绘**：需要生图而萌绘不可用 →
报错不出卡（不做无图降级）；满库角色不受影响。
"""
import asyncio
import importlib
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
        # 工坊后台试画/补图 job（webui_api 用）：job_id -> dict
        self._jobs: dict = {}
        self._reload_modules()

    # ---------- 生命周期 ----------

    def _reload_modules(self):
        """热重载自研子模块（依赖序）。main 里全部走模块属性访问（pool.xxx），
        reload 后自动生效，无需重新绑定函数对象。"""
        pkg = __package__
        for name in ("store", "card", "pool", "draw", "prompt_gen", "webui_api"):
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
        if status == "skipped_no_anima":
            return ("召唤老婆需要萌绘（ComfyUI 绘图）插件：请先安装并配置"
                    " astrbot_plugin_comfyui_anima（v7.7.58+）后再来哦")
        if status == "timeout":
            return (f"立绘生成超时了（>{int(self._cfg('draw_timeout', 120))}s），"
                    "萌绘可能正忙，稍后再试试吧")
        return f"立绘生成失败了：{err[:120]}。稍后再试一次吧"

    # ---------- 指令 ----------

    @filter.command("今日老婆", alias={"老婆", "jrlp"})
    async def waifu_cmd(self, event: AstrMessageEvent):
        """抽今日老婆：一人一天一位，种子决定，萌绘立绘卡片。"""
        if self._store is None or self._data_dir is None:
            yield event.plain_result("老婆殿还没布置好（插件未初始化完成），稍后再试～")
            return
        uid = str(event.get_sender_id() or "").strip()
        if not uid:
            yield event.plain_result("老婆殿没看清你是谁，稍后再试一次～")
            return
        gid = "" if event.is_private_chat() else str(event.get_group_id() or "")
        if gid and gid in self._disabled_groups():
            yield event.plain_result("本群已停用今日老婆（可在插件配置的「停用群」里修改）")
            return
        date = self._today()

        existing = self._store.get_draw(uid, date)
        if existing:
            card_path = str(existing.get("card_path") or "")
            if card_path and Path(card_path).is_file():
                yield event.chain_result([
                    Image.fromFileSystem(card_path),
                    Plain("这是你今天的老婆～明天再来见新的一位吧"),
                ])
                return
            # 卡图被清理过：立绘库里还有底图就重渲染一张（不重新生图）
            entry = pool.find_entry(self._load_packs(), existing["char_id"])
            rel = self._store.portrait_by_date(existing["char_id"], date) \
                or (self._store.list_portraits(existing["char_id"]) or [None])[-1]
            if entry is None or rel is None:
                yield event.plain_result("今天已经抽过老婆啦，但底图被清理了，明天再来吧")
                return
            card_path = await asyncio.to_thread(
                card.render_card, self._data_dir / rel["file"], entry,
                date_str=date, same_count=1, total_count=1,
                rerolled=bool(existing["rerolled"]),
                out_path=str(self._data_dir / "cards" / f"{uid}_{date}.png"),
                data_dir=self._data_dir,
            )
            self._store.update_draw_card(uid, date, card_path)
            yield event.chain_result([Image.fromFileSystem(card_path)])
            return

        # 首抽
        packs = self._load_packs()
        candidates = pool.candidates_for(
            packs, gid, str(self._cfg("group_pools", "") or ""),
            bool(self._cfg("allow_unverified", False)),
        )
        if not candidates:
            yield event.plain_result(
                "老婆库还是空的：请在 data/plugin_data/astrbot_plugin_moe_waifu/pools/ "
                "添加角色卷 JSON（或在卷里把角色标为 verified）"
            )
            return
        salt = str(self._cfg("salt", "moe-waifu"))
        entry = pool.pick_character(pool.derive_seed(date, uid, salt, 0), candidates)
        key = (uid, date)
        if key in self._inflight:
            yield event.plain_result("正在召唤中，稍等一下再看看～")
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
            yield event.plain_result("老婆殿还没布置好（插件未初始化完成），稍后再试～")
            return
        uid = str(event.get_sender_id() or "").strip()
        if not uid:
            yield event.plain_result("老婆殿没看清你是谁，稍后再试一次～")
            return
        gid = "" if event.is_private_chat() else str(event.get_group_id() or "")
        date = self._today()
        limit = int(self._cfg("reroll_daily_limit", 1))
        if limit <= 0:
            yield event.plain_result("今天的换老婆功能没有开启哦")
            return
        existing = self._store.get_draw(uid, date)
        if not existing:
            yield event.plain_result("今天还没抽过老婆，先发 /今日老婆 吧")
            return
        used = int(existing.get("nonce") or 0)
        if used >= limit:
            yield event.plain_result(f"今天的换老婆次数用完了（{limit} 次），明天再来吧")
            return
        packs = self._load_packs()
        candidates = pool.candidates_for(
            packs, gid, str(self._cfg("group_pools", "") or ""),
            bool(self._cfg("allow_unverified", False)),
        )
        if not candidates:
            yield event.plain_result("老婆库还是空的，换不了哦")
            return
        salt = str(self._cfg("salt", "moe-waifu"))
        entry = pool.pick_character(pool.derive_seed(date, uid, salt, used + 1), candidates)
        key = (uid, date)
        if key in self._inflight:
            yield event.plain_result("正在召唤中，稍等一下再看看～")
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
        yield event.plain_result(
            "♥ 萌萌今日老婆 ♥\n"
            "/今日老婆（别名 /老婆）：抽今天的老婆，萌绘立绘卡片，一人一天一位\n"
            "/换老婆：重新抽一位（受「每日换老婆次数」限制）\n\n"
            "角色库管理 / 试画 / 立绘库维护都在 AstrBot Dashboard 的插件 Page「工坊」里做"
            "（仅管理员）。\n"
            "本插件依赖萌绘（astrbot_plugin_comfyui_anima v7.7.58+）生成立绘，"
            "需要生图而萌绘不可用时会报错不出卡。\n"
            "立绘会沉淀进「立绘库」：每角色攒满配额后直接从库里取图，不再消耗绘图算力。\n"
            "角色库：data/plugin_data/astrbot_plugin_moe_waifu/pools/（一卷一 JSON）"
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
        decision = draw.decision_of(char_id, date, today_row, pool_size, quota)

        src = None
        rel_path = ""
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

        if src is None:  # generate
            seq = pool_size
            seed_int = pool.portrait_seed_int(char_id, date, seq, salt)
            if bool(self._cfg("announce_enabled", True)):
                await self._send_plain(event, "♥ 正在召唤你的今日老婆，请稍候……")
            # 提示词先产（v0.5.6）：img2img 由 LLM 生成 Qwen-Image-Edit 编辑指令
            # （失败自动回落本地模板），base/lora 为 tags + 变化维度
            draw_cfg = self._draw_cfg()
            prompt_text, prompt_src = await prompt_gen.build_generation_prompt(
                self.context, draw_cfg, entry, seed_int
            )
            logger.info(f"[{PLUGIN_NAME}] 立绘提示词（{prompt_src}）：{prompt_text[:80]}")
            raw_src, status, err, dur = await draw.generate_portrait(
                self.context, draw_cfg, entry, seed_int, event, prompt=prompt_text
            )
            self._store.log_job(char_id, date, "generated", status, uid,
                                entry["strategy_type"], seed_int, err, dur, raw_src or "")
            if raw_src is None:
                await self._send_plain(event, self._draw_error_text(status, err))
                return None
            rel_path = draw.archive_portrait(raw_src, self._data_dir, char_id, date, seq, seed_int)
            self._store.add_portrait(char_id, rel_path, seed_int, date, seq, entry["strategy_type"])
            src = self._data_dir / rel_path

        # 落库与计数（先 upsert 再查同款，含自己）
        # v0.9.0：角色名/作品/种子/立绘来源随签快照落库，历史记录不再依赖角色库反查；
        # upsert_draw 同时追加一条只写不改的抽签流水，换老婆前的角色也留得住。
        self._store.upsert_draw(
            uid, date, char_id, nonce, rerolled, nickname, gid, gname, platform,
            seed_hex=pool.derive_seed(date, uid, salt, nonce).hex(),
            char_name=str(entry.get("name") or ""),
            series=str(entry.get("series") or ""),
            portrait_file=rel_path,
            decision=decision,
        )
        total = self._store.bump_user_char(uid, char_id)
        self._store.bump_char_stats(char_id, date)
        same = self._store.same_group_today(gid, date, char_id)

        cards_dir = self._data_dir / "cards"
        cards_dir.mkdir(parents=True, exist_ok=True)
        card_path = await asyncio.to_thread(
            card.render_card, src, entry,
            date_str=date, same_count=same, total_count=total, rerolled=rerolled,
            out_path=str(cards_dir / f"{uid}_{date}.png"), data_dir=self._data_dir,
        )
        self._store.update_draw_card(uid, date, card_path)
        if decision != "generate":
            self._store.log_job(char_id, date, decision, "from_pool", uid,
                                entry["strategy_type"], None, "", 0, str(src))
        return card_path
