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

# 抽卡过程提示（v0.42.0：一律只说「正在抽卡」，绝不提绘图 / 参考图 / 出图）。
# 用户实报群里出现「今日这一签要照着参考图画，稍等片刻～」——那是旧版给
# `generate_i2i` 决策配的专属台词，把内部「这张卡是现画的还是从库里取的」
# 抖给了普通用户。抽卡就是抽卡，出图是插件内部实现，不该出现在播报文案里。
# 萌绘单张 20~120s，只说明「正在抽、大概要等」，不额外加戏。
_SUMMON_LINES = [
    "正在为你抽今日角色，请稍候……",
    "抽卡中，正在确认今日这一签，请稍等。",
    "正在抽取今日角色，马上就好。",
    "签已经抽好了，马上发给你～",
    "今日角色抽取中，稍后即可查看。",
    "正在准备今日的抽卡结果，请稍候。",
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


class WaifuApi:
    """跨插件契约（waifu.v1），供萌绘（astrbot-comfyui-anima）读取角色与主参考图。

    调用方：``context.get_registered_star("astrbot_plugin_moe_waifu").star_cls.api``。
    取不到、版本不匹配、或返回 ``status != "ok"`` 时必须安全降级，不能当角色库存在。
    """

    API_VERSION = "waifu.v1"

    def __init__(self, plugin: "MoeWaifuPlugin"):
        self._plugin = plugin

    def fingerprint(self) -> str:
        # v1.1：data[cid] 增补 avatar_thumb / portrait_thumb（缩略 data URL），主版不变
        return f"{self.API_VERSION}:1.1"

    async def main_ref_map(self) -> dict:
        """角色 → 主参考图映射（只取 strategy.ref，不碰 refs/立绘/三视图）。

        返回 ``{"status": "ok", "data": {char_id: {...}}, "missing": [char_id...]}``：
        - data[cid] = {"char_id","name","aliases","series","tags_en","ref"}；
          ``ref`` 是可直接交给萌绘图生图的绝对路径 / URL，可能为空；
        - missing = 主参考图为空或文件失效的角色 id（调用方 WebUI 据此提示）。
        """
        plugin = self._plugin
        try:
            return await asyncio.to_thread(self._map_sync)
        except Exception as e:
            return {"status": "error", "message": str(e), "data": {}, "missing": []}

    def _map_sync(self) -> dict:
        plugin = self._plugin
        store = getattr(plugin, "_store", None)
        data_dir = getattr(plugin, "_data_dir", None)
        if store is None or data_dir is None:
            return {"status": "error", "message": "老婆插件未初始化", "data": {}, "missing": []}
        packs = store.load_packs() or []
        data: dict = {}
        missing: list = []
        for pack in packs:
            for e in (pack.get("entries") or []):
                cid = str(e.get("id") or "").strip()
                if not cid:
                    continue
                st = e.get("strategy") if isinstance(e.get("strategy"), dict) else {}
                raw_ref = str(st.get("ref") or "").strip()
                ref = draw._ref_path(raw_ref, data_dir)
                aliases = e.get("aliases") or []
                if isinstance(aliases, str):
                    aliases = [x.strip() for x in re.split(r"[,，\n;；]+", aliases) if x.strip()]
                # v1.1：头像 / 立绘缩略图（给 Anima 角色卡页展示；带缓存，失败为 None）
                avatar_thumb = None
                portrait_thumbs: list[str] = []
                try:
                    from . import webui_api as _wa
                    _avatar_rel = str(e.get("avatar") or "").strip()
                    if _avatar_rel:
                        _ap = _wa._safe_data_file(plugin, _avatar_rel)
                        if _ap:
                            avatar_thumb = _wa._thumb_cached(_ap, 160)
                    # 只取第一张立绘做卡片缩略（按需再看详情，避免全量编码上千张）
                    for _p in (store.list_portraits(cid) or [])[:1]:
                        _prel = str(_p.get("file") or "").strip()
                        if _prel:
                            _pp = _wa._safe_data_file(plugin, _prel)
                            if _pp:
                                _t = _wa._thumb_cached(_pp, 420)
                                if _t:
                                    portrait_thumbs.append(_t)
                except Exception:
                    pass  # 缩略图失败不影响角色映射主数据
                data[cid] = {
                    "char_id": cid,
                    "name": str(e.get("name") or "").strip(),
                    "aliases": [str(x).strip() for x in aliases if str(x).strip()],
                    "series": str(e.get("series") or e.get("work") or "").strip(),
                    "tags_en": str(e.get("tags_en") or "").strip(),
                    "ref": ref,
                    "avatar_thumb": avatar_thumb,
                    "portrait_thumb": portrait_thumbs[0] if portrait_thumbs else None,
                }
                if not ref:
                    missing.append(cid)
        return {"status": "ok", "data": data, "missing": missing}


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
        # 签到联动后台任务（v0.55.0）：每条「签到」一个，terminate 时统一取消
        self._link_tasks: set = set()
        # 生图串行队列（v0.9.6）：萌绘一张 20~120s，多召唤并发时逐张画防打爆后端；
        # _gen_queue 是正在排队/作画的任务数，用于「前面还有 N 位」友好提示
        self._gen_lock = asyncio.Lock()
        self._gen_queue = 0
        # 工坊后台试画/补图 job（webui_api 用）：job_id -> dict
        self._jobs: dict = {}
        # 批量补图队列（v0.16.0）：进程内存态，重启即清
        self._batch = None
        # 跨插件契约（waifu.v1）：萌绘等插件经 star_cls.api 读取角色与主参考图
        self.api = WaifuApi(self)
        self._reload_modules()

    # ---------- 生命周期 ----------

    def _reload_modules(self):
        """热重载自研子模块（依赖序）。main 里全部走模块属性访问（pool.xxx），
        reload 后自动生效，无需重新绑定函数对象。"""
        pkg = __package__
        for name in ("store", "card", "pool", "draw", "prompt_gen",
                     "avatars", "batch_fill", "webui_api"):
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
            # v0.69.0：角色库真源切到 SQLite。启动顺序（关键，别乱动）：
            #   1) 先建 Store
            #   2) 老用户一次性把冻结的旧 JSON 底稿灌进 DB（DB 非空则跳过，
            #      绝不二次覆盖真源）
            #   3) DB 版卷收敛 / prune
            #   4) 种子对比补新（直接读写 DB）
            #   5) 挂读 provider + 写 save 钩子
            self._store = store.Store(self._data_dir / "waifu.db")
            try:
                mig = self._store.migrate_relational(
                    self._data_dir / "pools", self._data_dir, logger=logger,
                    only_if_empty=True)
                if mig.get("skipped_packs") == "db_nonempty":
                    logger.info(f"[{PLUGIN_NAME}] 角色库已是 DB 真源，跳过 JSON 灌库")
                else:
                    logger.info(f"[{PLUGIN_NAME}] 旧角色库 JSON 已一次性灌入 DB"
                                f"（characters={mig.get('characters')}）")
            except Exception:
                logger.error(f"[{PLUGIN_NAME}] 关系模型迁移失败\n{traceback.format_exc()}")
            # DB 版卷收敛（非标准卷按类型并入，旧 JSON 底稿冻结不动）
            try:
                from . import webui_api as _wa
                res = _wa.migrate_pack_collapse(self)
                if res.get("removed_packs"):
                    logger.info(f"[{PLUGIN_NAME}] 卷收敛完成：并入 "
                                f"{res.get('merged')} 个角色，移除卷 "
                                f"{res.get('removed_packs')}（旧 JSON 底稿冻结）")
            except Exception:
                logger.error(f"[{PLUGIN_NAME}] 卷收敛迁移失败\n{traceback.format_exc()}")
            # 清理不再存在的卷记录
            try:
                from . import webui_api as _wa
                keep = {t["key"] for t in _wa.load_type_registry(self)}
                pruned = self._store.prune_packs(keep)
                if pruned.get("removed"):
                    logger.info(f"[{PLUGIN_NAME}] 卷收敛清理旧卷记录：{pruned['removed']}")
            except Exception:
                logger.warning(f"[{PLUGIN_NAME}] stale 卷记录清理失败（忽略）\n{traceback.format_exc()}")
            # 种子对比补新：DB 真源，读/写都走 store，旧 JSON 文件不碰
            try:
                pool.install_seed_pool(
                    self._data_dir / "pools", bundled, logger,
                    read_pack=self._store.load_pack_data,
                    save_pack=self._store.save_pack_data)
            except Exception:
                logger.error(f"[{PLUGIN_NAME}] 种子补新失败\n{traceback.format_exc()}")
            # 读路径：DB 真源（空表 / 异常时 pool 内部回退 JSON，仅极端兜底）
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
            try:  # v0.67.2：头像指针自愈（v0.67.0 跨卷移动丢指针事故的补救）
                from . import webui_api as _webui
                _webui.heal_avatar_pointers(self)
            except Exception:
                logger.warning(f"[{PLUGIN_NAME}] 头像指针自愈失败（忽略）\n{traceback.format_exc()}")
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
        # 签到联动任务先取消：它们可能在等生图，留着会继续往旧实例上写库
        for task in list(getattr(self, "_link_tasks", ())):
            task.cancel()
        self._link_tasks.clear()
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

    def _allow_private(self) -> bool:
        """私聊是否允许抽卡（v0.54.2，默认关闭：只允许群聊抽）。"""
        return bool(self._cfg("allow_private_draw", False))

    def _same_scope(self) -> str:
        """卡面统计口径：`all`（所有群共用一份，默认）/ `group`（仅当前群）。"""
        scope = str(self._cfg("same_stat_scope", "all") or "all").strip().lower()
        return "group" if scope == "group" else "all"

    def _today_stats(self, existing: dict) -> tuple:
        """按当前口径算（今日同款人数, 今日第几个抽到她），出错回落 (1, 1)。

        调用前该人当天的 draws 行必须已存在（含自己）。
        """
        try:
            return self._store.today_char_stats(
                str(existing.get("char_id") or ""), str(existing.get("date") or ""),
                str(existing.get("user_id") or ""),
                str(existing.get("group_id") or ""), self._same_scope())
        except Exception:
            logger.warning(f"[{PLUGIN_NAME}] 统计同款人数失败（按 1 处理）\n"
                           f"{traceback.format_exc()}")
            return 1, 1

    def _draw_cfg(self) -> dict:
        return {
            "draw_width": self._cfg("draw_width", 700),
            "draw_height": self._cfg("draw_height", 1200),
            "draw_timeout": self._cfg("draw_timeout", 120),
            # v0.66.0：萌绘忙时先排队等空闲的预算（秒，0=关）——等待不占 draw_timeout
            "anima_queue_wait": self._cfg("anima_queue_wait", 600),
            "draw_workflow": self._cfg("draw_workflow", ""),
            "draw_negative_enabled": self._cfg("draw_negative_enabled", True),
            "draw_negative_prompt": self._cfg("draw_negative_prompt", ""),
            "img2img_workflow": self._cfg("img2img_workflow", ""),
            "img2img_denoise": self._cfg("img2img_denoise", 0.65),
            # v0.59.0：头像生成边长（方形直出，默认 1024）
            "avatar_size": self._cfg("avatar_size", 1024),
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
        """抽卡过程提示：排队时告知前面还有几位；否则随机一句抽卡口径。

        v0.42.0：决策不再影响文案——`generate_i2i` 只是内部出图路径，
        专属的「照着参考图画」台词已删除。`decision` 保留入参只为兼容
        既有调用与测试签名。
        """
        if queued > 0:
            return random.choice(_QUEUE_LINES).format(n=queued)
        return random.choice(_SUMMON_LINES)

    # ---------- 签到联动（v0.55.0） ----------
    #
    # 用户只发一条「签到」（/签到 或裸词）就把三个插件串起来：
    #   1) 本插件当编排者——当天还没抽老婆就先抽一位并发送；
    #   2) 紧接着叫星语插件补一张（同样只在当天首签时发）；
    #   3) get_px 的 /签到 完全不动：这里只 create_task、不 stop_event、不 yield，
    #      两个入口都立即返回，那边照常渲染并发出自己的签到卡，两套出图并行。
    #
    # 当天已抽过的静默跳过（不重发旧卡）；用户手发 /今日老婆 仍走原来的重发逻辑。

    # 同时接受裸词「签到」与带唤醒前缀的「/签到」——AstrBot 多数版本在 filter
    # 前会剥掉 wake_prefix，但个别平台/配置下 message_str 仍是原文，两种都认。
    CHECKIN_LINK_RE = r"^/?签到$"
    CHECKIN_LINK_EXTRA = "_moe_checkin_link"
    STAR_WHISPER_PLUGIN = "astrbot_plugin_moe_star_whisper"

    def checkin_link_ready(self) -> bool:
        """给别的插件看：本插件是否接管「签到」联动。"""
        try:
            return bool(self._cfg("checkin_link_enabled", False))
        except Exception:
            return False

    # priority 提到 999：确保抢在任何插件的 /签到 command（含高优先级实现）之前
    # 建好后台任务；get_px 随后 stop_event 也不影响已经 create_task 的联动。
    @filter.regex(CHECKIN_LINK_RE, priority=999)
    async def checkin_link_entry(self, event: AstrMessageEvent):
        """「签到」联动入口：只建后台任务，立即返回（绝不阻塞签到卡）。"""
        if not self.checkin_link_ready():
            return
        if self._store is None or self._data_dir is None:
            return
        if (
            bool(self._cfg("checkin_link_require_wake", False))
            and not event.is_at_or_wake_command
        ):
            return  # 只认 /签到 或 @机器人，不理群里随口说的「签到」
        if event.get_extra(self.CHECKIN_LINK_EXTRA):
            return  # 同一条消息已经被别处接管
        event.set_extra(self.CHECKIN_LINK_EXTRA, True)
        event.should_call_llm(False)
        task = asyncio.create_task(self._run_checkin_link(event))
        self._link_tasks.add(task)
        task.add_done_callback(self._link_tasks.discard)

    async def _run_checkin_link(self, event: AstrMessageEvent):
        """严格串行：老婆出完再轮到星语；任一步失败都不影响另一步。"""
        try:
            delay = max(0, int(self._cfg("checkin_link_delay", 0) or 0))
            if delay:
                await asyncio.sleep(min(delay, 60))
            await self._linked_waifu(event)
            if bool(self._cfg("checkin_link_include_star", True)):
                await self._linked_star(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error(f"[{PLUGIN_NAME}] 签到联动失败\n{traceback.format_exc()}")

    async def _linked_waifu(self, event: AstrMessageEvent) -> None:
        uid = str(event.get_sender_id() or "").strip()
        if not uid:
            return
        gid = "" if event.is_private_chat() else str(event.get_group_id() or "")
        if gid and gid in self._disabled_groups():
            return
        if not gid and not self._allow_private():
            return
        date = self._today()
        if bool(self._cfg("checkin_link_only_first", True)):
            if self._store.get_draw(uid, date) is not None:
                return  # 今天已经抽过：不重发、不重抽
            if (uid, date) in self._inflight:
                return  # 手发 /今日老婆 正在抽：让那次自己发
        async for res in self.waifu_cmd(event):
            await self._send_linked_result(event, res)

    async def _linked_star(self, event: AstrMessageEvent) -> None:
        try:
            star = self.context.get_registered_star(self.STAR_WHISPER_PLUGIN)
        except Exception:
            return
        if star is None or not getattr(star, "activated", False):
            return
        linked = getattr(getattr(star, "star_cls", None), "linked_fortune", None)
        if linked is None:
            logger.info(
                f"[{PLUGIN_NAME}] 星语插件版本过旧，签到联动跳过（需 v1.13.0+）"
            )
            return
        try:
            async for res in linked(event):
                await self._send_linked_result(event, res)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error(
                f"[{PLUGIN_NAME}] 签到联动星语失败\n{traceback.format_exc()}"
            )

    async def _send_linked_result(self, event: AstrMessageEvent, res) -> None:
        """联动结果走 context 主动发送，避开与 get_px respond 共用 event 的路径。"""
        chain = getattr(res, "chain", None)
        if not chain:
            return
        try:
            await self.context.send_message(
                event.unified_msg_origin, MessageChain(chain=list(chain))
            )
        except Exception:
            logger.error(
                f"[{PLUGIN_NAME}] 签到联动发送失败\n{traceback.format_exc()}"
            )

    # ---------- 指令 ----------

    @filter.command("今日老婆", alias={"jrlp"})
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
        # v0.54.2：私聊抽卡默认关闭（配置 allow_private_draw）
        if not gid and not self._allow_private():
            yield event.plain_result("私聊暂未开放抽卡，请在群里发送 /今日老婆 吧～")
            return
        date = self._today()

        existing = self._store.get_draw(uid, date)
        if existing:
            same, rank = self._today_stats(existing)
            # 累计次数以 user_char_counts 为准（老行的 total_count 快照是 0）
            total = self._store.user_char_count(uid, str(existing.get("char_id") or ""))
            card_path = str(existing.get("card_path") or "")
            card_ok = bool(card_path) and Path(card_path).is_file()
            # v0.54.2（用户）：卡面不再是快照——重发前先看「今日同款人数 / 今日第
            # 几个抽到她」有没有变，变了就用库存底图重渲染再发；没变直接重发省一次渲染。
            stale = (int(existing.get("same_count") or 0) != same
                     or int(existing.get("today_rank") or 0) != rank
                     or total <= 0
                     # v0.54.6：旧版式画的卡（含已下线的「重抽」标）重发时重画一次
                     or int(existing.get("card_ver") or 0) < int(card.CARD_VER))
            if card_ok and not stale:
                # v0.51.1（用户）：重复抽只重发卡图，不带「明天可以再抽一位」
                # 这类解释性文案——今天抽没抽过、要不要换老婆，用户自己清楚。
                yield event.chain_result([Image.fromFileSystem(card_path)])
                return
            # 卡图被清理 / 统计变了：立绘库里还有底图就重渲染一张（不重新生图）
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
                    # v0.71.11：卡面文件名带 nonce——换签各自成文件保留，旧卡
                    # 不再被覆盖（总览时间线 / 记录详情流水都能回看每张卡）
                    card_path = await asyncio.to_thread(
                        card.render_card, self._data_dir / rel["file"], entry,
                        date_str=date, same_count=same, total_count=max(1, total),
                        today_rank=rank,
                        rerolled=bool(existing["rerolled"]),
                        out_path=str(self._data_dir / "cards"
                                     / f"{uid}_{date}_{int(existing.get('nonce') or 0)}.png"),
                        data_dir=self._data_dir,
                        seal_pos=self._cfg("card_seal_pos", "name"),
                        scrim=self._cfg("card_scrim", 100),
                    )
                except Exception:
                    logger.error(f"[{PLUGIN_NAME}] 卡图重渲染失败\n{traceback.format_exc()}")
                    yield event.plain_result("抽卡失败，请稍后再试。")
                    return
                self._store.update_draw_card(
                    uid, date, card_path, same_count=same, today_rank=rank,
                    total_count=max(1, total), card_ver=card.CARD_VER)
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
            disabled_tags=set(pool.parse_user_tags(
                str(self._cfg("disabled_user_tags", "") or ""))),
        )
        if not candidates:
            logger.warning(
                f"[{PLUGIN_NAME}] 抽卡失败：没有可抽角色"
                "（pools/ 下没有启用的角色卷，或卷内角色均未 verified）")
            yield event.plain_result("抽卡失败，暂时没有可抽的角色，请稍后再试。")
            return
        # v0.68.1：pool 模式先剔除库空角色（自动换到有图的，不算换老婆次数）
        candidates = self._drawable_candidates(candidates)
        if not candidates:
            yield event.plain_result(
                "抽卡失败：当前「只用立绘库」模式下，没有任何角色有立绘。"
                "请先在工坊给角色补图，或把出图方式改成「混合 / 只用图生图模板」。")
            return
        # v0.48.0：等概率基线 + 近期重复降权（`draw_recency`），见 _pick_character
        entry = self._pick_character(candidates, uid, date, 0)
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
        # v0.54.2：私聊抽卡关闭时，换老婆同样拒绝（否则可以先私聊抽再换）
        if not gid and not self._allow_private():
            yield event.plain_result("私聊暂未开放抽卡，请在群里发送 /换老婆 吧～")
            return
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
            disabled_tags=set(pool.parse_user_tags(
                str(self._cfg("disabled_user_tags", "") or ""))),
        )
        if not candidates:
            logger.warning(f"[{PLUGIN_NAME}] 重抽失败：没有可抽角色")
            yield event.plain_result("抽卡失败，暂时没有可抽的角色。")
            return
        # v0.68.1：pool 模式同样剔除库空角色（自动换到有图的）
        candidates = self._drawable_candidates(candidates)
        if not candidates:
            yield event.plain_result(
                "换老婆失败：当前「只用立绘库」模式下，没有任何角色有立绘。"
                "请先在工坊给角色补图，或把出图方式改成「混合 / 只用图生图模板」。")
            return
        # v0.48.0：同一天更早的签也进历史（权重 ×0.15）→ 「换老婆」更容易换到别人
        entry = self._pick_character(candidates, uid, date, used + 1)
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
            "/今日老婆（别名 /jrlp）：抽取今天缘定的那位，一人一天一位\n"
            "/换老婆：重新抽一位（每日有次数限制）\n\n"
            "每张卡图都有专属印象语与评级徽章，每位角色都有专属立绘。\n"
            "祝今天也遇见心动的那位。"
        )

    # ---------- 公平性：历史（v0.48.0） ----------

    def _draw_history(self, uid: str, date: str = "", nonce: int = 0) -> list:
        """该用户的抽签流水（旧→新），排除当前这一签（同 date 且 nonce ≥ 本次）。

        排除自身是为了「同一天多次计算 / 换老婆 / 工坊重渲染」结果稳定：历史只由
        **更早的签**构成，算多少次都一样；已写库的本次签不会反过来改变自己的结果。
        """
        try:
            rows = self._store.draw_history(uid)
        except Exception:
            logger.warning(f"[{PLUGIN_NAME}] 读取抽签流水失败（按无历史处理）\n"
                           f"{traceback.format_exc()}")
            return []
        if not date:
            return rows
        out = []
        for r in rows:
            if str(r.get("date") or "") == date and int(r.get("nonce") or 0) >= int(nonce):
                continue
            out.append(r)
        return out

    def _drawable_candidates(self, candidates: list) -> list:
        """v0.68.1：pool（只用立绘库）模式先剔除立绘库为空的角色。

        用户口径：只用立绘库时不画新图，抽到没立绘的角色就自动换一位有图的，
        且**不算 /换老婆 次数**。过滤放在选角色之前——候选里全是库里有图的人，
        种子照常抽，天然满足这两点。mix/img2img 会现画补库，不过滤。
        """
        if str(self._cfg("gen_source_mode", "mix") or "mix") != "pool":
            return candidates
        if not self._store:
            return candidates
        return [e for e in candidates
                if self._store.count_portraits(str(e.get("id") or "")) > 0]

    def _pick_character(self, candidates: list, uid: str, date: str, nonce: int) -> dict:
        """抽卡 = 等概率基线 + 近期重复降权（`draw_recency` 百分比，0 = 关闭）。

        权重只跟**这个人自己的历史**有关，所以「同一天同一位」的语义不变，别人抽到
        谁与你无关；同角色永远有权重 > 0，不会被永久挡住。
        """
        seed = pool.derive_seed(date, uid, str(self._cfg("salt", "moe-waifu")),
                                nonce, self._store.get_epoch(uid))
        try:
            pct = float(self._cfg("draw_recency", 100) or 0)
        except (TypeError, ValueError):
            pct = 0.0
        weights = None
        if pct > 0:
            hist = [(r.get("date"), r.get("char_id"))
                    for r in self._draw_history(uid, date, nonce)]
            weights = pool.recency_weights(candidates, hist, date, pct / 100.0)
        return pool.pick_character_weighted(seed, candidates, weights)

    def _portrait_cycle_state(self, uid: str, char_id: str, date: str,
                              nonce: int) -> tuple:
        """(该角色文件真实存在的池, 这个用户用过的文件[旧→新])。"""
        rows = self._store.list_portraits(char_id) or []
        files = [str(r["file"]) for r in rows
                 if str(r.get("file") or "")
                 and (self._data_dir / str(r["file"])).is_file()]
        used = [str(r.get("portrait_file") or "")
                for r in self._draw_history(uid, date, nonce)
                if str(r.get("char_id") or "") == char_id and r.get("portrait_file")]
        return files, used

    # ---------- 核心产出 ----------

    @staticmethod
    def _platform_name(event: AstrMessageEvent) -> str:
        """平台短名（aiocqhttp / telegram …）。

        v0.54.0：老版本写的是 `str(event.platform)`——那是 PlatformMetadata
        整个 dataclass 的 repr，记录页「平台」列显示成
        `PlatformMetadata(name='aiocqhttp', description='适用于 OneBot …')`
        一大串无关描述。官方取值接口是 `event.get_platform_name()`，取不到再
        退回 `platform_meta.name`，最后兜底解析旧 repr。
        """
        name = ""
        try:
            name = str(event.get_platform_name() or "")
        except Exception:
            name = ""
        if not name:
            try:
                name = str(event.platform_meta.name or "")
            except Exception:
                name = ""
        if not name:
            name = str(getattr(event, "platform", "") or "")
        return store.normalize_platform(name)

    async def _group_name_of(self, event: AstrMessageEvent, gid: str) -> str:
        """群名兜底：aiocqhttp 的群消息事件里 group_name 恒为空。

        OneBot V11 的群消息事件只带 group_id（不带群名），AstrBot 适配器把它
        直接塞进 `message_obj.group.group_name`，于是老记录页整列都显示「私聊」。
        这里按需向适配器要一次 get_group_info；失败 / 超时一律返回空串——
        抽卡不能因为查群名失败而中断。

        v0.54.3：先看事件里现成的群名；再带 self_id 试一次，失败去掉 self_id 再试
        （不同适配器对多余参数的容忍度不一）；仍失败就把真实原因写进日志（warning，
        之前是 debug 级，排查时完全看不见）。
        """
        if not gid:
            return ""
        try:
            g = getattr(getattr(event, "message_obj", None), "group", None)
            name = str(getattr(g, "group_name", "") or "").strip()
            if name:
                return name
        except Exception:
            pass
        bot = getattr(event, "bot", None)
        if bot is None:
            logger.debug(f"[{PLUGIN_NAME}] 群名查询跳过：事件没有 bot 属性（gid={gid}）")
            return ""
        api_gid = int(gid) if str(gid).isdigit() else gid
        # ⚠️ self_id 必须原样透传（aiocqhttp 用它路由到具体连接，int/str 不通用；
        # 早先这里 str() 了一把，路由不上直接报错 → 群名永远读不到）。
        self_id = getattr(getattr(event, "message_obj", None), "self_id", None)
        variants = []
        if self_id:
            variants.append({"group_id": api_gid, "self_id": self_id})
        variants.append({"group_id": api_gid})
        last_err = ""
        for params in variants:
            try:
                info = await asyncio.wait_for(
                    bot.call_action("get_group_info", **params), timeout=5)
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                continue
            if isinstance(info, dict):
                name = str(info.get("group_name") or "").strip()
                if name:
                    return name
                last_err = f"返回里没有 group_name：{str(info)[:200]}"
            else:
                last_err = f"返回类型异常：{type(info).__name__}"
        logger.warning(f"[{PLUGIN_NAME}] 群名获取失败（gid={gid}）：{last_err}")
        return ""

    async def _produce(self, event: AstrMessageEvent, entry: dict, uid: str, date: str,
                       nonce: int, rerolled: bool) -> str | None:
        """立绘库决策 → 出图/取图 → 计数落库 → 渲染出卡。失败返回 None（已发错误提示）。"""
        gid = "" if event.is_private_chat() else str(event.get_group_id() or "")
        gname = ""
        try:
            gname = str(event.get_group_name() or "") if gid else ""
        except Exception:
            pass
        # v0.54.0：aiocqhttp 群消息不带群名 → 主动问一次适配器（顺便修好
        # 「记录页群列全显示私聊」）；查不到就留空，前端回落显示群号。
        if gid and not gname:
            gname = await self._group_name_of(event, gid)
        nickname = ""
        try:
            nickname = str(event.get_sender_name() or "")
        except Exception:
            pass
        platform = self._platform_name(event)

        char_id = entry["id"]
        salt = str(self._cfg("salt", "moe-waifu"))
        quota = max(1, int(self._cfg("portraits_per_char", 5)))
        today_row = self._store.portrait_by_date(char_id, date)
        pool_size = self._store.count_portraits(char_id)
        # v0.48.0：立绘按「用户 × 角色」遍历——同角色重复抽到时优先换一张没用过的
        pool_files, used_files = self._portrait_cycle_state(uid, char_id, date, nonce)
        used_set = set(used_files)
        unused_files = [f for f in pool_files if f not in used_set]
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
        # v0.48.0：还有这个人没抽到过的库存图 → 优先取库存（立绘不重复，也不烧算力）。
        # 只在「本来要文生图」时接管：图生图重绘是用户显式要新图，不抢。
        prefer_note = ""
        if (decision == "generate"
                and bool(self._cfg("prefer_unused_portrait", True))
                and unused_files):
            decision = "from_pool"
            prefer_note = (f"优先用没抽到过的库存立绘"
                           f"（该角色还有 {len(unused_files)} 张你没见过）")
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
        if decision == "skip":
            # v0.68.1 兜底：pool 模式库空。正常抽卡已在选角阶段剔除库空角色，
            # 走到这里只剩重渲染 / 并发删库的边缘情形——不画新图，提示后退出。
            logger.warning(f"[{PLUGIN_NAME}] 抽卡跳过：{char_id} 立绘库为空（pool 模式不画新图）")
            await self._send_plain(event, "这位角色暂时没有立绘，已跳过，请换一位或稍后再试。")
            return None
        elif decision == "from_pool":
            # v0.48.0：在这个用户还没抽到过的图里确定性挑一张；整轮抽完自动进下一轮
            rel_path, _used_n, _new_round = pool.portrait_cycle_pick(
                pool_files, used_files, seed_bytes)
            src = (self._data_dir / rel_path) if rel_path else None
            if src is None or not src.is_file():   # 库文件被手动删了：回退为重新生成
                src = None
                rel_path = ""
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
            # v0.39.0：抽卡画师模式——开关开启且本次现画时，提示词由 LLM 画师
            # 整体创作（不经预设维度池）；LLM 任何失败自动回落池子链路，
            # 抽卡永不因画师中断。库存命中根本不走这里（直接复用立绘），
            # 画师不会触发也不消耗 LLM。
            draw_cfg = self._draw_cfg()
            prompt_text, prompt_src = "", ""
            if bool(self._cfg("artist_draw_enabled", False)):
                try:
                    prompt_text, prompt_src = await prompt_gen.build_artist_prompt(
                        self.context, draw_cfg, entry, seed_int
                    )
                except Exception as exc:
                    logger.warning(f"[{PLUGIN_NAME}] 画师出词失败，回落池子链路：{exc}")
            if not prompt_text:
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
                    seed_int, force_single=(entry["strategy_type"] == "img2img"),
                    keep=prompt_gen.keep_dims_of(entry)),
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
        if prefer_note:
            note += "；" + prefer_note
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
        # v0.54.2：口径可配置（所有群共用 / 仅当前群），返回 (今日同款人数, 今日第几个)
        same, rank = self._store.today_char_stats(
            char_id, date, uid, gid, self._same_scope())

        cards_dir = self._data_dir / "cards"
        cards_dir.mkdir(parents=True, exist_ok=True)
        try:
            # v0.71.11：卡面文件名带 nonce——换签各自的卡都保留可回看
            card_path = await asyncio.to_thread(
                card.render_card, src, entry,
                date_str=date, same_count=same, total_count=total, today_rank=rank,
                rerolled=rerolled,
                out_path=str(cards_dir / f"{uid}_{date}_{int(nonce or 0)}.png"),
                data_dir=self._data_dir,
                seal_pos=self._cfg("card_seal_pos", "name"),
                scrim=self._cfg("card_scrim", 100),
            )
        except Exception:
            # 渲染异常只写日志：群里只回一句通用失败提示，不把栈信息抛给用户
            logger.error(f"[{PLUGIN_NAME}] 卡图渲染失败\n{traceback.format_exc()}")
            await self._send_plain(event, "抽卡失败，请稍后再试。")
            return None
        # 顺手记下本次渲染用的统计快照 + 版式版本：重发卡时用它判断要不要重渲染
        self._store.update_draw_card(uid, date, card_path, same_count=same,
                                     today_rank=rank, total_count=total,
                                     card_ver=card.CARD_VER)
        if decision != "generate":
            self._store.log_job(char_id, date, decision, "from_pool", uid,
                                entry["strategy_type"], None, "", 0, str(src))
        return card_path
