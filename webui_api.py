# -*- coding: utf-8 -*-
"""萌萌今日老婆 · 角色库工坊后端（AstrBot 插件 Page 桥接 API）。

路由前缀 ``/astrbot_plugin_moe_waifu``，前端经 ``window.AstrBotPluginPage`` 桥调用
（Dashboard 登录墙内，仅管理员使用）。返回信封遵循 AstrBot 桥接约定：
  成功 → {"status": "ok", "data": ...}
  失败 → {"status": "error", "message": "..."}

试画走后台 job + 前端轮询（萌绘单张 20~120s，不能占着 HTTP 请求等）。
试画没有真实聊天事件——萌绘 handler 会校验 event 类型，这里合成一个最小
AstrMessageEvent 桩（platform=webui）；构造失败回落 None（萌绘自行回落
_last_event，再不行就报错，绝不影响主流程）。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import re
import shutil
import time
import traceback
from pathlib import Path

try:  # AstrBot 运行期
    from astrbot.api import logger
except Exception:  # pragma: no cover - 本地平铺调试
    import logging

    logger = logging.getLogger("moe_waifu")

try:  # quart 是 AstrBot 的运行期依赖
    from quart import request
except Exception:  # pragma: no cover
    request = None  # type: ignore

try:  # 双形态导入（包内正常加载 / 平铺调试）
    from . import pool, draw, prompt_gen, batch_fill
except ImportError:  # pragma: no cover
    import pool  # type: ignore
    import draw  # type: ignore
    import prompt_gen  # type: ignore
    import batch_fill  # type: ignore

ROUTE_PREFIX = "/astrbot_plugin_moe_waifu"
PLUGIN_NAME = "astrbot_plugin_moe_waifu"

# 后台试画 job 上限（超出时先清理最旧的已完成 job）
_JOB_KEEP = 40


def ok(data=None) -> dict:
    return {"status": "ok", "data": data if data is not None else {}}


def err(message: str) -> dict:
    return {"status": "error", "message": str(message)}


def _q(name: str, default=None):
    if request is None:
        return default
    try:
        return request.args.get(name, default)
    except Exception:
        return default


async def _body() -> dict:
    if request is None:
        return {}
    try:
        data = await request.get_json(silent=True)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _pools_dir(plugin) -> Path:
    return Path(plugin._data_dir) / "pools"


def _load_pack_file(plugin, pack_name: str) -> tuple | None:
    """按卷名读原始卷文件，返回 (path, data)；不存在返回 None。"""
    path = _pools_dir(plugin) / f"{pack_name}.json"
    if not path.is_file():
        return None
    try:
        return path, _json_load(path)
    except Exception as exc:
        logger.error(f"[{PLUGIN_NAME}] 卷文件解析失败 {path.name}: {exc}")
        return None


def _json_load(path: Path) -> dict:
    import json

    return json.loads(Path(path).read_text(encoding="utf-8"))


def _json_save(path: Path, data) -> None:
    import json

    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _version_of() -> str:
    """读 metadata.yaml 的 version（带 v 前缀）。

    ⚠️ 不能找 plugin._plugin_version —— 那是 main.py 的模块级函数而非类方法，
    hasattr 永远 False，页面版本号就会显示成 v?（v0.3.1 及以前真机如此）。
    """
    try:
        text = (Path(__file__).resolve().parent / "metadata.yaml").read_text(encoding="utf-8")
        m = re.search(r"(?m)^\s*version:\s*v?([0-9][^\s#]*)", text)
        return f"v{m.group(1).strip()}" if m else ""
    except Exception:
        return ""


_THUMB_CACHE: dict = {}
# v0.18.2：立绘缩略也进这份缓存（切角色要重取一整墙图），给个上限防常驻内存
# 无限涨。每条上限约 40~60KB（420px JPEG base64），600 条约 30MB 封顶。
_THUMB_CACHE_MAX = 600


def _trim_thumb_cache() -> None:
    """超上限按插入顺序丢最旧的（dict 保序；近似 LRU 足够，不必精确）。"""
    while len(_THUMB_CACHE) > _THUMB_CACHE_MAX:
        try:
            _THUMB_CACHE.pop(next(iter(_THUMB_CACHE)))
        except (StopIteration, KeyError):  # pragma: no cover - 并发删同名键
            break


def _thumb_cached(path, max_w: int = 160) -> str | None:
    """缩略 data URL（带 mtime+size 缓存）：/pools 每次装载都会全量重取，别每次重编码。

    缓存键含路径与宽度；stamp 同时看 mtime 与文件大小——图片被原地覆盖
    但 mtime 精度不足（1s 内 / copy2 保留 mtime）时，旧 data URL 不会继续命中
    （v0.10.1：设完封面仍是旧图的次要隐患）。

    v0.18.2：`/portraits/list` 也改走这里——切角色要重新编码一整墙立绘（每张
    420px JPEG，几十毫秒），真机上就是「换角色后要等 1~3 秒」的主要开销。
    """
    p = Path(path)
    if not p.is_file():
        return None
    key = f"{p}|{max_w}"
    try:
        stat = p.stat()
        stamp = (stat.st_mtime, stat.st_size)
    except Exception:
        return None
    cached = _THUMB_CACHE.get(key)
    if cached and cached[0] == stamp:
        return cached[1]
    durl = _thumb_data_url(str(p), max_w)
    _THUMB_CACHE[key] = (stamp, durl)
    _trim_thumb_cache()
    return durl


def _safe_data_file(plugin, path: str) -> Path | None:
    """校验图片路径可用作封面/参考图：绝对路径必须落在 data 目录内，
    相对路径按 data 目录解析；文件必须存在。返回可用的 Path 或 None。"""
    p = str(path or "").strip()
    if not p:
        return None
    try:
        fp = Path(p)
        if not fp.is_absolute():
            fp = Path(plugin._data_dir) / fp
        fp = fp.resolve()
        data_root = Path(plugin._data_dir).resolve()
        if data_root not in fp.parents and fp != data_root:
            return None
        return fp if fp.is_file() else None
    except Exception:
        return None


def _works_meta_path(plugin) -> Path:
    return Path(plugin._data_dir) / "works_meta.json"


def _load_works_meta(plugin) -> dict:
    try:
        data = _json_load(_works_meta_path(plugin))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_works_meta(plugin, data: dict) -> None:
    _json_save(_works_meta_path(plugin), data)


def _char_cover_path(plugin, entry: dict) -> str | None:
    """角色封面路径：entry.cover 优先，其次第一张立绘。返回绝对路径或 None。"""
    cover = str(entry.get("cover") or "").strip()
    if cover:
        fp = _safe_data_file(plugin, cover)
        if fp is not None:
            return str(fp)
    if plugin._store:
        rows = plugin._store.list_portraits(entry["id"])
        if rows:
            fp = Path(plugin._data_dir) / rows[0]["file"]
            if fp.is_file():
                return str(fp)
    return None


def _set_entry_field(plugin, cid: str, mutate) -> str | None:
    """找到包含该角色的卷文件，mutate(e_normalized, raw_entry) 原地改，写回。
    返回卷名；找不到返回 None。"""
    for pack in pool.load_packs(_pools_dir(plugin), logger):
        loaded = _load_pack_file(plugin, pack["pack"])
        if loaded is None:
            continue
        path, data = loaded
        entries = data.get("entries") if isinstance(data.get("entries"), list) else []
        changed = False
        for raw in entries:
            e = pool.normalize_entry(raw, pack["pack"])
            if e and e["id"] == cid:
                mutate(e, raw)
                changed = True
                break
        if changed:
            _json_save(path, data)
            return pack["pack"]
    return None


def _thumb_data_url(image_path: str, max_w: int = 420) -> str | None:
    """图片 → 缩略 data URL（JPEG）；失败返回 None。"""
    try:
        from PIL import Image

        with Image.open(image_path) as img:
            w, h = img.size
            if w > max_w:
                img = img.resize((max_w, max(1, round(h * max_w / w))), Image.LANCZOS)
            buf = io.BytesIO()
            img.convert("RGB").save(buf, "JPEG", quality=82)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


def _synthetic_event():
    """工坊试画用的最小 AstrMessageEvent 桩（platform=webui）。

    萌绘 handler 会 ``isinstance(event, AstrMessageEvent)`` 校验，无效时回落
    它自己的 _last_event（可能是任何会话的最后一条消息）——桩事件保证
    「工坊里试画」不借用聊天会话上下文。构造失败返回 None（行为退化为萌绘默认）。
    """
    try:
        from types import SimpleNamespace

        from astrbot.core.platform.astrbot_message import AstrBotMessage
        from astrbot.core.platform.astr_message_event import AstrMessageEvent
        from astrbot.core.platform.platform_metadata import PlatformMetadata

        msg = AstrBotMessage()
        try:
            from astrbot.core.platform.astrbot_message import MessageType

            msg.type = MessageType.OtherMessage
        except Exception:
            msg.type = "OtherMessage"
        msg.self_id = "moe_waifu_workshop"
        msg.session_id = "moe_waifu_workshop"
        msg.message_id = f"waifu_workshop_{int(time.time() * 1000)}"
        msg.sender = SimpleNamespace(user_id="waifu_workshop", nickname="工坊", avatar_url="")
        msg.message = []
        msg.message_str = ""
        msg.raw_message = ""
        meta = PlatformMetadata(name="webui", description="萌萌今日老婆 工坊", id="moe_waifu_workshop")
        return AstrMessageEvent("", msg, meta, "moe_waifu_workshop")
    except Exception:
        logger.warning(f"[{PLUGIN_NAME}] 试画桩事件构造失败\n{traceback.format_exc()}")
        return None


# ---------- 角色库 ----------

async def h_pools(plugin) -> dict:
    packs = pool.load_packs(_pools_dir(plugin), logger)
    counts: dict = {}
    thumbs: dict = {}
    cover_paths: dict = {}
    out_packs = []
    for pack in packs:
        entries = []
        for raw in pack["entries"]:
            entry = pool.normalize_entry(raw, pack["pack"])
            if entry is None:
                continue
            if entry["id"] not in counts:
                rows = plugin._store.list_portraits(entry["id"]) if plugin._store else []
                counts[entry["id"]] = len(rows)
                # 卡片视图封面（v0.5.0）：角色 cover 优先 → 首张立绘
                cover_fp = _char_cover_path(plugin, entry)
                if cover_fp:
                    # 420（v0.7.0）：角色卡封面在卡上显示约 260px，160 太糊
                    thumbs[entry["id"]] = _thumb_cached(cover_fp, 420)
                cover_paths[entry["id"]] = cover_fp
            entry["cover_path"] = cover_paths.get(entry["id"])
            entries.append(entry)
        out_packs.append({"pack": pack["pack"], "title": pack["title"],
                          "enabled": pack["enabled"], "entries": entries})
    anima_ready = draw._get_tool_handler(plugin.context, "comfyui_draw") is not None
    return ok({
        "packs": out_packs,
        "counts": counts,
        "thumbs": thumbs,
        "cover_paths": cover_paths,
        "version": _version_of(),
        "anima_ready": anima_ready,
        "quota": int(plugin._cfg("portraits_per_char", 5)),
        "draw_timeout": int(plugin._cfg("draw_timeout", 120)),
        "allow_unverified": bool(plugin._cfg("allow_unverified", False)),
    })


async def h_save_entry(plugin) -> dict:
    body = await _body()
    pack_name = str(body.get("pack") or "").strip()
    entry = body.get("entry")
    if not pack_name or not isinstance(entry, dict):
        return err("缺少 pack 或 entry 字段")
    normalized = pool.normalize_entry(entry, pack_name)
    if normalized is None:
        return err("条目不完整：id / name / tags_en 必填")
    loaded = _load_pack_file(plugin, pack_name)
    if loaded is None:
        return err(f"卷不存在：{pack_name}")
    path, data = loaded
    entries = data.get("entries") if isinstance(data.get("entries"), list) else []
    # 跨卷 id 查重（编辑自身除外）
    for pack in pool.load_packs(_pools_dir(plugin), logger):
        if pack["pack"] == pack_name:
            continue
        for raw in pack["entries"]:
            other = pool.normalize_entry(raw, pack["pack"])
            if other and other["id"] == normalized["id"]:
                return err(f"id 与卷 {pack['pack']} 中的角色重复：{normalized['id']}")
    replaced = False
    for i, raw in enumerate(entries):
        existing = pool.normalize_entry(raw, pack_name)
        if existing and existing["id"] == normalized["id"]:
            # 表单只托管部分字段：整体替换会静默清空 strategy.refs / cover，
            # 用户「改个名字保存」后参考图就全不见了（v0.9.10 修复）。
            entries[i] = pool.merge_saved_entry(raw, normalized)
            replaced = True
            break
    if not replaced:
        entries.append(normalized)
    data["entries"] = entries
    _json_save(path, data)
    return ok({"replaced": replaced, "entry": normalized})


async def h_import_pack(plugin) -> dict:
    """批量导入角色：把粘贴/上传的 JSON 落成一卷（新建或按 id 合并进已有卷）。

    接受两种 JSON 形态：
      - 整卷对象：{"pack": "卷名", "title": "...", "entries": [ {...}, ... ]}
      - 纯数组：[{...}, ...]  → 用请求里的 pack 字段（或默认 "imported"）当卷名
    每条需含 id / name / tags_en（走 normalize_entry 校验，不合格整条跳过）。

    v0.23.0：查重范围从「目标卷」扩到**全库**——同 id 在别的卷已存在也算
    重复（导入的旧卷后来并进新卷，界面上就会一人两份）。overwrite 默认关闭，
    已存在一律跳过；开启时只用导入内容更新基础字段（name/series/tags 等），
    `strategy` / `cover` / `verified` / `id` / `pack` 全部保留本地值，参考图
    绑定与封面不会丢。同名同作品但 id 不同的不自动合并，交给「重复角色」
    工具处理，避免误把两个不同角色并成一条。
    """
    body = await _body()
    text = str(body.get("text") or "").strip()
    pack_override = str(body.get("pack") or "").strip()
    overwrite = bool(body.get("overwrite", False))
    if not text:
        return err("请粘贴或上传 JSON 文本")
    try:
        obj = json.loads(text)
    except Exception as exc:
        return err("JSON 解析失败：" + str(exc)[:120])
    if isinstance(obj, dict):
        pack_name = pack_override or str(obj.get("pack") or "").strip()
        title = str(obj.get("title") or pack_name or "导入卷")
        entries = obj.get("entries")
    elif isinstance(obj, list):
        pack_name = pack_override or "imported"
        title = pack_override or "导入卷"
        entries = obj
    else:
        return err("JSON 需为「角色数组」或「{pack, entries}」对象")
    if not pack_name:
        return err("缺少卷名：请在 JSON 里给 pack 字段，或填写卷名输入框")
    if not isinstance(entries, list) or not entries:
        return err("entries 为空或非数组")
    good, skipped = [], []
    for raw in entries:
        e = pool.normalize_entry(raw, pack_name)
        if e:
            good.append(e)
        else:
            skipped.append({"id": str((raw or {}).get("id")
                                      or (raw or {}).get("name") or "?"),
                            "reason": "字段不合法"})
    if not good:
        return err("没有合法条目（每条需 id / name / tags_en）")

    pools_dir = _pools_dir(plugin)
    pools_dir.mkdir(parents=True, exist_ok=True)
    # 全库索引（id / 名字+作品）→ (卷名, 条目下标)；同时缓存卷文件内容，
    # 覆盖写回时按卷批量保存，避免逐条读写把同一卷改花。
    by_id: dict = {}
    by_ns: dict = {}
    pack_data: dict = {}
    for p in pool.load_packs(pools_dir, logger):
        loaded = _load_pack_file(plugin, p["pack"])
        if loaded is None:
            continue
        path, data = loaded
        if not isinstance(data, dict):
            continue
        if not isinstance(data.get("entries"), list):
            data["entries"] = []
        pack_data[p["pack"]] = (path, data)
        for idx, raw in enumerate(data["entries"]):
            ne = pool.normalize_entry(raw, p["pack"])
            if not ne:
                continue
            by_id.setdefault(ne["id"], (p["pack"], idx))
            ns = (str(ne["name"]).strip().lower(),
                  str(ne["series"] or "").strip().lower())
            if ns[0]:
                by_ns.setdefault(ns, (p["pack"], idx))

    target_path = pools_dir / f"{pack_name}.json"
    created = not target_path.exists()
    if pack_name not in pack_data:
        pack_data[pack_name] = (target_path, {
            "pack": pack_name, "title": title, "enabled": True, "entries": []})
    _, target_data = pack_data[pack_name]
    if not isinstance(target_data.get("entries"), list):
        target_data["entries"] = []
    target_data["pack"] = pack_name
    target_data["title"] = target_data.get("title") or title
    target_data["enabled"] = bool(target_data.get("enabled", True))

    added = updated = dup_skipped = 0
    dirty: set = set()
    for e in good:
        cid = e["id"]
        ns = (str(e["name"]).strip().lower(),
              str(e["series"] or "").strip().lower())
        if cid in by_id:
            loc = by_id[cid]
            if overwrite and loc[0] in pack_data:
                _, data = pack_data[loc[0]]
                lst = data["entries"]
                if 0 <= loc[1] < len(lst):
                    cur_e = pool.normalize_entry(lst[loc[1]], loc[0])
                    # 索引可能因脏数据（非 dict 条目被装载层过滤）错位，
                    # 写回前再对一次 id，绝不误改别的角色。
                    if cur_e and cur_e["id"] == cid:
                        lst[loc[1]] = pool.merge_imported_entry(lst[loc[1]], e)
                        updated += 1
                        dirty.add(loc[0])
                        continue
            dup_skipped += 1
            skipped.append({"id": cid, "reason": "全库已存在"
                            + ("" if overwrite else "（未开启覆盖）")})
            continue
        if ns[0] and ns in by_ns:
            dup_skipped += 1
            skipped.append({"id": cid,
                            "reason": "同名同作品已存在（请用重复角色工具处理）"})
            continue
        target_data["entries"].append(e)
        idx = len(target_data["entries"]) - 1
        by_id[cid] = (pack_name, idx)
        if ns[0]:
            by_ns.setdefault(ns, (pack_name, idx))
        added += 1
        dirty.add(pack_name)
    for name in dirty:
        path, data = pack_data[name]
        _json_save(path, data)
    return ok({
        "pack": pack_name, "created": created,
        "imported": added, "updated": updated, "duplicates": dup_skipped,
        "total": len(good), "skipped": skipped, "overwrite": overwrite,
    })


async def h_delete_entry(plugin) -> dict:
    body = await _body()
    pack_name = str(body.get("pack") or "").strip()
    cid = str(body.get("id") or "").strip()
    loaded = _load_pack_file(plugin, pack_name)
    if loaded is None or not cid:
        return err("缺少 pack 或 id，或卷不存在")
    path, data = loaded
    entries = data.get("entries") if isinstance(data.get("entries"), list) else []
    kept = []
    removed = 0
    for raw in entries:
        e = pool.normalize_entry(raw, pack_name)
        if e and e["id"] == cid:
            removed += 1
            continue
        kept.append(raw)
    if not removed:
        return err(f"卷 {pack_name} 中找不到 {cid}")
    data["entries"] = kept
    _json_save(path, data)
    orphan = plugin._store.count_portraits(cid) if plugin._store else 0
    return ok({"removed": removed, "portraits_left": orphan})


def _dedupe_snapshot(plugin) -> list:
    """重复角色预览：重复组 + 每条资产信息 + 建议保留下标（只读）。

    keep 规则：资产最全（立绘 > 参考图 > 封面 > 已验证）优先，同分取组内
    靠前（items 已按 pack/index 排序，结果确定）。不改任何数据。
    """
    store = plugin._store
    out = []
    for g in pool.duplicate_groups(pool.load_packs(_pools_dir(plugin), logger)):
        items = []
        for m in g["items"]:
            e = m["entry"]
            pc = store.count_portraits(e["id"]) if store else 0
            refs = len(pool.ref_paths(e))
            cover = bool(str(e.get("cover") or "").strip())
            items.append({
                "pack": m["pack"], "index": m["index"],
                "id": e["id"], "name": e["name"],
                "series": str(e["series"] or ""),
                "verified": bool(e["verified"]),
                "portrait_count": pc, "refs_count": refs,
                "has_cover": cover, "score": pool.asset_score(e, pc),
                # modal slot 内插值调 .join()/.map() 会打断整块渲染（v0.16.0
                # 实测），展示文本一律后端拼好，模板只做纯字段插值。
                "asset_text": f"立绘 {pc}、参考图 {refs}"
                              + ("、封面" if cover else ""),
            })
        keep = max(range(len(items)), key=lambda i: (items[i]["score"], -i))
        first = items[0] if items else {}
        out.append({"key": g["key"], "reasons": g["reasons"],
                    "reasons_text": " + ".join(g["reasons"]),
                    "title": f"{first.get('name', '')}（{first.get('series') or '无作品'}）",
                    "items": items, "keep": keep})
    return out


async def h_relational_status(plugin) -> dict:
    """关系模型阶段 1 只读状态：各表数量与立绘 asset_id 覆盖情况。

    阶段 1 只建表 + 迁移，不改现有读写路径；这个接口供工坊核对地基是否
    就绪（assets 是否覆盖全部图片、characters 是否只含约定作品）。
    """
    if not plugin._store:
        return err("插件未初始化完成")
    return ok({"status": plugin._store.relational_status()})


async def h_duplicates(plugin) -> dict:
    """扫描全库重复角色（只读预览，不改动数据）。"""
    groups = _dedupe_snapshot(plugin)
    return ok({"groups": groups, "total_groups": len(groups),
               "total_dup": sum(len(g["items"]) - 1 for g in groups)})


async def h_dedupe(plugin) -> dict:
    """按预览结果清理重复角色（需 confirm + keys），保留资产最全的一条。

    安全口径：① 执行前重新扫描，keep/drop 由后端按资产分数决定，不信任前端
    传来的保留项；② 被移除条目的参考图/封面并入保留条目；③ 立绘行按
    char_id 迁移到保留 id（图片文件一个不删），不产生孤儿立绘；④ 改动前的
    全部卷文件备份到 data/dedupe_backups/<时间戳>/（不在 pools/ 内，不会被
    当成角色卷装载）。
    """
    body = await _body()
    if not bool(body.get("confirm", False)):
        return err("请先扫描预览并确认后再清理")
    wanted = {str(k) for k in (body.get("keys") or []) if str(k).strip()}
    if not wanted:
        return err("没有选择要清理的重复组")
    groups = [g for g in _dedupe_snapshot(plugin) if g["key"] in wanted]
    if not groups:
        return err("选中的重复组已不存在，请重新扫描")
    pack_data: dict = {}
    for p in pool.load_packs(_pools_dir(plugin), logger):
        loaded = _load_pack_file(plugin, p["pack"])
        if loaded is None:
            continue
        path, data = loaded
        if isinstance(data, dict) and isinstance(data.get("entries"), list):
            pack_data[p["pack"]] = (path, data)
    stamp = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000}"
    backup_dir = Path(plugin._data_dir) / "dedupe_backups" / stamp
    backup_dir.mkdir(parents=True, exist_ok=True)
    for name, (_p, data) in pack_data.items():
        (backup_dir / f"{name}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    removed = moved = 0
    drops: set = set()
    touched: set = set()
    for g in groups:
        items = g["items"]
        keep = items[g["keep"]]
        if keep["pack"] not in pack_data:
            continue
        base_list = pack_data[keep["pack"]][1]["entries"]
        if not (0 <= keep["index"] < len(base_list)):
            continue
        keep_raw = base_list[keep["index"]]
        for i, it in enumerate(items):
            if i == g["keep"] or it["pack"] not in pack_data:
                continue
            loc = (it["pack"], it["index"])
            if loc in drops:
                continue
            plist = pack_data[it["pack"]][1]["entries"]
            if not (0 <= it["index"] < len(plist)):
                continue
            drop_raw = plist[it["index"]]
            ne = pool.normalize_entry(drop_raw, it["pack"])
            if not ne or ne["id"] != it["id"]:
                continue
            keep_raw = pool.merge_entry_assets(keep_raw, drop_raw)
            if it["id"] != keep["id"] and plugin._store:
                moved += plugin._store.reassign_portraits(it["id"], keep["id"])
            drops.add(loc)
            removed += 1
            touched.add(it["pack"])
        base_list[keep["index"]] = keep_raw
        touched.add(keep["pack"])
    for name in touched:
        path, data = pack_data[name]
        for idx in sorted((i for p, i in drops if p == name), reverse=True):
            if 0 <= idx < len(data["entries"]):
                data["entries"].pop(idx)
        _json_save(path, data)
    return ok({"removed": removed, "moved_portraits": moved,
               "groups": len(groups), "backup": str(backup_dir)})


async def h_set_pack(plugin) -> dict:
    body = await _body()
    pack_name = str(body.get("pack") or "").strip()
    loaded = _load_pack_file(plugin, pack_name)
    if loaded is None:
        return err(f"卷不存在：{pack_name}")
    path, data = loaded
    data["enabled"] = bool(body.get("enabled", True))
    _json_save(path, data)
    return ok({"pack": pack_name, "enabled": data["enabled"]})


async def h_set_verified(plugin) -> dict:
    body = await _body()
    cid = str(body.get("id") or "").strip()
    verified = bool(body.get("verified", False))
    if not cid:
        return err("缺少 id")
    for pack in pool.load_packs(_pools_dir(plugin), logger):
        if pack["pack"] == "":  # pragma: no cover
            continue
        loaded = _load_pack_file(plugin, pack["pack"])
        if loaded is None:
            continue
        path, data = loaded
        entries = data.get("entries") if isinstance(data.get("entries"), list) else []
        changed = False
        for raw in entries:
            e = pool.normalize_entry(raw, pack["pack"])
            if e and e["id"] == cid:
                raw["verified"] = verified
                changed = True
                break
        if changed:
            _json_save(path, data)
            return ok({"id": cid, "verified": verified})
    return err(f"角色库里找不到 {cid}")


# ---------- 立绘库 ----------

async def h_portraits_index(plugin) -> dict:
    if not plugin._store:
        return err("插件未初始化完成")
    name_of: dict = {}
    for pack in pool.load_packs(_pools_dir(plugin), logger):
        for raw in pack["entries"]:
            e = pool.normalize_entry(raw, pack["pack"])
            if e:
                name_of.setdefault(e["id"], (e["name"], e["color"], pack["pack"]))
    grouped_ids: set = set(name_of.keys())
    # 也把「有立绘但条目已删」的角色列出来（孤立图，可在此清理）
    for row in _all_portrait_rows(plugin):
        grouped_ids.add(row["char_id"])
    chars = []
    for cid in sorted(grouped_ids):
        rows = plugin._store.list_portraits(cid)
        first = rows[0] if rows else None
        meta = name_of.get(cid) or (cid, "#8A8A9A", "")
        chars.append({
            "id": cid,
            "name": meta[0],
            "color": meta[1],
            "pack": meta[2],
            "count": len(rows),
            "thumb": _thumb_cached(str(plugin._data_dir / first["file"]), 420) if first else None,
        })
    return ok({"chars": chars})


def _all_portrait_rows(plugin) -> list:
    """全部立绘行（孤立图发现用）。"""
    try:
        return plugin._store.list_portraits_all()
    except Exception:
        return []


async def h_portraits_list(plugin) -> dict:
    body = await _body()
    cid = str(body.get("char_id") or _q("char_id", "") or "").strip()
    if not cid or not plugin._store:
        return err("缺少 char_id")
    items = []
    # v0.21.0：连停用行一起返回（active 字段区分），前端默认隐藏、可切换查看
    # v0.23.1：seq 是唯一序列（删图不回收、会跳号），另给一个按现存档案
    # 顺排的 display_no 供界面显示；排序 / 种子 / 文件名一律仍用 seq。
    for idx, row in enumerate(plugin._store.list_portraits(cid, include_disabled=True)):
        full = Path(plugin._data_dir) / row["file"]
        items.append({
            "file": row["file"], "seq": row["seq"],
            # v0.24.0：全局图片唯一编号（assets.id），与角色内 seq 分开
            "asset_id": int(row.get("asset_id") or 0),
            "display_no": idx + 1, "date": row["date"],
            "seed": row["seed"], "exists": full.is_file(),
            # abs：绝对路径（v0.6.0）——立绘可被选为图生图源；draw 侧
            # Path(ref).exists() 按 CWD 解析相对路径，必须传绝对路径
            "abs": str(full),
            # v0.18.2：走缓存（原先每次都重新编码 420px JPEG，切一次角色就是
            # 一整墙重编码——真机「换角色要等 1~3 秒」的主要开销）
            "thumb": _thumb_cached(full, 420) if full.is_file() else None,
            # v0.15.0：详情弹窗用（旧行 prompt 为 null）
            "created_at": row["created_at"],
            "prompt": row["prompt"],
            "theme": row["theme"],
            "strategy": row["strategy"],
            # v0.20.0：变化维度（JSON 串，旧行为空）与图片尺寸（未知为 0）
            "dims": row.get("dims") or "",
            "width": int(row.get("width") or 0),
            "height": int(row.get("height") or 0),
            # v0.21.0：软删除标记（0 = 已停用）
            "active": int(row.get("active") or 0),
        })
    return ok({"char_id": cid, "items": items})


def _safe_portrait_rel(rel: str) -> bool:
    """立绘相对路径白名单形状：portraits/<char_id>/<file>.png，禁路径穿越。"""
    if not re.fullmatch(r"portraits/[^/\\]+/[^/\\]+", str(rel or "").replace("\\", "/")):
        return False
    return ".." not in rel


# ---------- 立绘 ↔ 参考图互转（v0.9.1） ----------
# 旧行为「加入参考图库」只是把立绘路径追加进 strategy.refs：两个列表指向同一个
# 文件，立绘没消失，删立绘又会把参考图变死链。这里改成真·移动语义：
#   立绘 → 参考图：另存 refs/ → 改写所有引用 → 删原立绘行与文件
#   参考图 → 立绘：另存 portraits/ → 从 refs 解绑 → 无其它引用时删原文件
# 两边列表互斥，文件各自独立，删一边不影响另一边。

def _norm_ref(plugin, path: str) -> str:
    """引用路径规范化成「相对 data 目录的 POSIX 串」。

    历史数据里 refs 混着绝对路径（上传走的 res.path）和相对路径（绑立绘走的
    it.file），不规范化就没法比较是不是同一张图。URL / 目录外字符串原样返回。
    """
    s = str(path or "").strip().replace("\\", "/")
    if not s or re.match(r"^[a-zA-Z]+://", s):
        return s
    fp = Path(s)
    if not fp.is_absolute():
        fp = Path(plugin._data_dir) / fp
    try:
        return fp.resolve().relative_to(Path(plugin._data_dir).resolve()).as_posix()
    except Exception:
        return s


def _walk_pack_entries(plugin, mutate) -> int:
    """遍历所有卷的**原始**条目；mutate(raw) 返回 True 表示该条有改动。
    有改动的卷整体写回，返回被改写的卷数。"""
    touched = 0
    for pack in pool.load_packs(_pools_dir(plugin), logger):
        loaded = _load_pack_file(plugin, pack["pack"])
        if loaded is None:
            continue
        path, data = loaded
        entries = data.get("entries") if isinstance(data.get("entries"), list) else []
        changed = False
        for raw in entries:
            if isinstance(raw, dict) and mutate(raw):
                changed = True
        if changed:
            _json_save(path, data)
            touched += 1
    return touched


def _rewrite_path_refs(plugin, old: str, new: str) -> int:
    """把条目 strategy.ref / refs、entry.cover 及作品封面里指向 old 的引用
    全部改成 new。用在「文件搬家后保活引用」，返回改动的条目数。"""
    target = _norm_ref(plugin, old)
    if not target or target == _norm_ref(plugin, new):
        return 0
    count = 0

    def mutate(raw):
        nonlocal count
        hit = False
        st = raw.get("strategy") if isinstance(raw.get("strategy"), dict) else None
        if st is not None:
            if _norm_ref(plugin, st.get("ref")) == target:
                st["ref"] = new
                hit = True
            # 出卡图生图源跟着改指新文件（v0.10.0）
            if _norm_ref(plugin, st.get("draw_ref")) == target:
                st["draw_ref"] = new
                hit = True
            refs = st.get("refs")
            if isinstance(refs, list):
                replaced = []
                for x in refs:
                    v = new if _norm_ref(plugin, x) == target else x
                    # 绝对/相对两种写法可能同时存在，改写后要顺手去重
                    if v not in replaced:
                        replaced.append(v)
                if replaced != refs:
                    st["refs"] = replaced
                    hit = True
        if _norm_ref(plugin, raw.get("cover")) == target:
            raw["cover"] = new
            hit = True
        if hit:
            count += 1
        return hit

    _walk_pack_entries(plugin, mutate)
    meta = _load_works_meta(plugin)
    meta_hit = False
    for slot in meta.values():
        if isinstance(slot, dict) and _norm_ref(plugin, slot.get("cover")) == target:
            slot["cover"] = new
            meta_hit = True
    if meta_hit:
        _save_works_meta(plugin, meta)
        count += 1
    return count


def _path_is_referenced(plugin, rel: str) -> bool:
    """该路径是否仍被任何角色条目 / 作品封面引用（只读探测，不写回）。"""
    target = _norm_ref(plugin, rel)
    found = False

    def probe(raw):
        nonlocal found
        st = raw.get("strategy") if isinstance(raw.get("strategy"), dict) else {}
        vals = [st.get("ref"), st.get("draw_ref"), raw.get("cover")]
        vals += list(st.get("refs") or []) if isinstance(st.get("refs"), list) else []
        if any(_norm_ref(plugin, v) == target for v in vals):
            found = True
        return False

    _walk_pack_entries(plugin, probe)
    for slot in _load_works_meta(plugin).values():
        if isinstance(slot, dict) and _norm_ref(plugin, slot.get("cover")) == target:
            found = True
    return found


def _copy_into_refs(plugin, src: Path) -> str:
    """按内容寻址另存一份到 data/refs/，返回相对路径；同内容复用不重复堆文件。"""
    raw = src.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()[:16]
    suffix = (src.suffix or ".png").lower()
    if suffix not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        suffix = ".png"
    refs_dir = Path(plugin._data_dir) / "refs"
    refs_dir.mkdir(parents=True, exist_ok=True)
    dest = refs_dir / f"mv_{digest}{suffix}"
    if not dest.is_file():
        dest.write_bytes(raw)
    return f"refs/{dest.name}"


def _unlink_portrait(plugin, rel: str) -> None:
    (Path(plugin._data_dir) / rel).unlink(missing_ok=True)
    plugin._store.delete_portrait(rel)


def _move_portrait_to_ref(plugin, rel: str, cid: str = "") -> dict:
    """立绘 → 参考图（同步 core，可直测）。失败抛 ValueError。"""
    if not _safe_portrait_rel(rel):
        raise ValueError("非法立绘路径")
    src = Path(plugin._data_dir) / rel
    if not src.is_file():
        raise ValueError("立绘文件不存在")
    cid = str(cid or "").strip() or rel.split("/")[1]
    if pool.find_entry(pool.load_packs(_pools_dir(plugin), logger), cid) is None:
        raise ValueError(f"角色库里找不到「{cid}」，无法绑定参考图")
    new_rel = _copy_into_refs(plugin, src)
    # 历史脏数据：同一个立绘路径可能早被老版「加入参考图库」绑进 refs，
    # 先统一改到新路径，再去重追加，避免出现两条引用同一张图。
    rewritten = _rewrite_path_refs(plugin, rel, new_rel)
    # v0.20.0：立绘行的生成信息（提示词/主题/种子/尺寸）随文件带走
    if plugin._store:
        try:
            prow = plugin._store.portrait_by_file(rel)
            if prow:
                plugin._store.set_ref_meta(
                    new_rel, prompt=prow.get("prompt"),
                    theme=str(prow.get("theme") or ""), seed=prow.get("seed"),
                    source="moved",
                    width=int(prow.get("width") or 0),
                    height=int(prow.get("height") or 0))
        except Exception:
            logger.warning(f"[{PLUGIN_NAME}] 参考图生成信息记录失败（不影响转移）\n"
                           f"{traceback.format_exc()}")

    def bind(_e, raw):
        st = raw.get("strategy") if isinstance(raw.get("strategy"), dict) else {}
        refs = [str(x) for x in (st.get("refs") or []) if str(x or "").strip()]
        if new_rel not in refs:
            refs.append(new_rel)
        st["refs"] = refs
        raw["strategy"] = st

    if _set_entry_field(plugin, cid, bind) is None:
        raise ValueError(f"角色库里找不到 {cid}")
    _unlink_portrait(plugin, rel)
    logger.info(f"[{PLUGIN_NAME}] 立绘转参考图：{rel} → {new_rel}")
    return {"from": rel, "to": new_rel, "char_id": cid, "rewritten_refs": rewritten}


def _move_ref_to_portrait(plugin, cid: str, path: str) -> dict:
    """参考图 → 立绘（同步 core，可直测）。失败抛 ValueError。"""
    norm = _norm_ref(plugin, path)
    if not norm.startswith("refs/"):
        raise ValueError("这张参考图不在 refs/ 目录里；若它本来就是立绘，请直接移除绑定")
    src = _safe_data_file(plugin, path)
    if src is None:
        raise ValueError("参考图文件不存在或不在数据目录内")
    if pool.find_entry(pool.load_packs(_pools_dir(plugin), logger), cid) is None:
        raise ValueError(f"角色库里找不到「{cid}」")
    if not plugin._store:
        raise ValueError("插件未初始化完成")
    date = plugin._today()
    seq = plugin._store.alloc_portrait_seq(cid)
    seed = int(hashlib.sha256(src.read_bytes()).hexdigest()[:8], 16) % 100000
    new_rel = draw.archive_portrait(str(src), plugin._data_dir, cid, date, seq, seed)
    plugin._store.add_portrait(cid, new_rel, seed, date, seq, "moved")

    main_ref_cleared = False

    def mutate(raw):
        # 只解绑「当前角色」：同一张 refs 可能被别的角色共用，牵连别人会让
        # 那边的参考图凭空消失（v0.9.1 实测踩到）。
        nonlocal main_ref_cleared
        if str(raw.get("id") or "") != cid:
            return False
        hit = False
        st = raw.get("strategy") if isinstance(raw.get("strategy"), dict) else None
        if st is not None:
            refs = st.get("refs")
            if isinstance(refs, list):
                kept = [x for x in refs if _norm_ref(plugin, x) != norm]
                if kept != refs:
                    st["refs"] = kept
                    hit = True
            if _norm_ref(plugin, st.get("ref")) == norm:
                st["ref"] = ""   # 主参考跟着解绑，避免指向已搬走的文件
                main_ref_cleared = True
                hit = True
            if _norm_ref(plugin, st.get("draw_ref")) == norm:
                st["draw_ref"] = ""   # 出卡源同理，不能指向已搬走的文件
                hit = True
        if _norm_ref(plugin, raw.get("cover")) == norm:
            raw["cover"] = new_rel  # 封面只是展示，图还在，改指新立绘
            hit = True
        return hit

    _walk_pack_entries(plugin, mutate)
    # v0.12.5：**不再删除原 refs 文件**。转移 = 复制进立绘库 + 解绑参考图，
    # 原图留在 refs/ 里成为「未绑定图片」，误操作也能在参考图区一键找回。
    # （用户反馈过「生成后参考图消失」；宁可多占一份空间也不静默删文件。）
    logger.info(f"[{PLUGIN_NAME}] 参考图转立绘：{norm} → {new_rel}（原图保留在 refs/）")
    return {"from": norm, "to": new_rel, "char_id": cid,
            "file_removed": False, "main_ref_cleared": main_ref_cleared}


async def h_portrait_move_to_ref(plugin) -> dict:
    body = await _body()
    try:
        return ok(await asyncio.to_thread(
            _move_portrait_to_ref, plugin,
            str(body.get("file") or "").strip(), str(body.get("id") or "").strip()))
    except ValueError as exc:
        return err(str(exc))
    except Exception as exc:
        logger.warning(f"[{PLUGIN_NAME}] 立绘转参考图失败\n{traceback.format_exc()}")
        return err(f"转移失败：{exc}")


async def h_ref_move_to_portrait(plugin) -> dict:
    body = await _body()
    try:
        return ok(await asyncio.to_thread(
            _move_ref_to_portrait, plugin,
            str(body.get("id") or "").strip(), str(body.get("path") or "").strip()))
    except ValueError as exc:
        return err(str(exc))
    except Exception as exc:
        logger.warning(f"[{PLUGIN_NAME}] 参考图转立绘失败\n{traceback.format_exc()}")
        return err(f"转移失败：{exc}")


def _disable_portrait_core(plugin, rel: str) -> dict:
    """v0.21.0：软删除（停用）。文件与行都保留——已抽到这张的用户仍可溯源
    （draws / draw_events 里的 portrait_file 指向不变），只是该图不再参与
    抽卡（当日复用/满库取图）与配额。"""
    if not _safe_portrait_rel(rel):
        raise ValueError("非法路径")
    if not plugin._store:
        raise ValueError("插件未初始化完成")
    if plugin._store.set_portrait_active(rel, False) == 0:
        raise ValueError("立绘行不存在")
    return {"disabled": rel}


def _restore_portrait_core(plugin, rel: str) -> dict:
    """v0.21.0：恢复停用的立绘（重新参与抽卡与配额）。"""
    if not _safe_portrait_rel(rel):
        raise ValueError("非法路径")
    if not plugin._store:
        raise ValueError("插件未初始化完成")
    if plugin._store.set_portrait_active(rel, True) == 0:
        raise ValueError("立绘行不存在")
    return {"restored": rel}


def _delete_portrait_core(plugin, rel: str) -> dict:
    """彻底删除（v0.21.0 起仅对已停用的立绘开放，且走回收站可手动找回）；
    若它还被参考图引用（老版脏数据），先另存到 refs/ 保活。"""
    if not _safe_portrait_rel(rel):
        raise ValueError("非法路径")
    full = Path(plugin._data_dir) / rel
    relocated = ""
    if full.is_file() and _path_is_referenced(plugin, rel):
        relocated = _copy_into_refs(plugin, full)
        _rewrite_path_refs(plugin, rel, relocated)
    moved, trash_dir = _trash_files(plugin, [rel])
    if moved != 1:
        raise RuntimeError("移入回收站失败")
    plugin._store.delete_portrait(rel)
    return {"deleted": rel, "relocated_to": relocated, "trash": trash_dir}


async def h_portrait_delete(plugin) -> dict:
    """v0.21.0：单张删除 = 软删除（停用），文件与行保留、可恢复、可溯源。"""
    body = await _body()
    try:
        return ok(await asyncio.to_thread(
            _disable_portrait_core, plugin, str(body.get("file") or "").strip()))
    except (ValueError, RuntimeError) as exc:
        return err(str(exc))
    except Exception as exc:
        logger.warning(f"[{PLUGIN_NAME}] 停用立绘失败\n{traceback.format_exc()}")
        return err(f"停用失败：{exc}")


async def h_portrait_restore(plugin) -> dict:
    body = await _body()
    try:
        return ok(await asyncio.to_thread(
            _restore_portrait_core, plugin, str(body.get("file") or "").strip()))
    except (ValueError, RuntimeError) as exc:
        return err(str(exc))
    except Exception as exc:
        logger.warning(f"[{PLUGIN_NAME}] 恢复立绘失败\n{traceback.format_exc()}")
        return err(f"恢复失败：{exc}")


async def h_portrait_delete_hard(plugin) -> dict:
    """彻底删除（文件进回收站 + 删行）。只对已停用的立绘开放，且必须带
    confirm=true（前端两段确认 + 服务端强校验，对齐调试区危险操作口径）。"""
    body = await _body()
    if not body.get("confirm"):
        return err("缺少 confirm=true（彻底删除是不可逆操作）")
    rel = str(body.get("file") or "").strip()
    try:
        if not plugin._store:
            return err("插件未初始化完成")
        row = plugin._store.portrait_by_file(rel)
        if row is None:
            return err("立绘行不存在")
        if int(row.get("active") or 0) == 1:
            return err("该立绘仍在启用中：请先停用（软删除）再彻底删除")
        return ok(await asyncio.to_thread(_delete_portrait_core, plugin, rel))
    except (ValueError, RuntimeError) as exc:
        return err(str(exc))
    except Exception as exc:
        logger.warning(f"[{PLUGIN_NAME}] 彻底删除立绘失败\n{traceback.format_exc()}")
        return err(f"彻底删除失败：{exc}")


# ---------- 试画 / 补图（后台 job + 轮询） ----------

async def h_refs_upload(plugin) -> dict:
    """工坊上传参考图（bridge.upload → Dashboard 以 multipart 字段 file 转发到这里）。

    存入 data 目录 refs/（持久保留，路径可直接填进条目 strategy.ref）。
    """
    if request is None:
        return err("无请求上下文")
    try:
        files = await request.files
    except Exception:
        files = getattr(request, "files", None)
    f = files.get("file") if files else None
    if f is None:
        return err("缺少 file 字段（multipart）")
    suffix = Path(f.filename or "").suffix.lower()
    if suffix not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        return err("仅支持 png/jpg/jpeg/webp/gif")
    raw = f.read()
    if not raw:
        return err("空文件")
    if len(raw) > 15 * 1024 * 1024:
        return err("图片超过 15MB 上限")
    refs_dir = Path(plugin._data_dir) / "refs"
    refs_dir.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^0-9A-Za-z._-]", "_", Path(f.filename or "ref").stem)[:40] or "ref"
    fname = f"upload_{int(time.time())}_{stem}{suffix}"
    (refs_dir / fname).write_bytes(raw)
    return ok({"path": str(refs_dir / fname), "rel": f"refs/{fname}",
               "size": len(raw), "name": fname})


async def h_job_start(plugin) -> dict:
    body = await _body()
    kind = str(body.get("kind") or "try").strip()
    cid = str(body.get("id") or "").strip()
    # v0.7.5：no_archive=true 时 refill 生成完不自动归档（配合 /jobs/to_ref
    # 转成参考图，不进立绘列表）
    no_archive = bool(body.get("no_archive"))
    if kind not in ("try", "refill"):
        return err("kind 只能是 try / refill")
    entry = pool.find_entry(pool.load_packs(_pools_dir(plugin), logger), cid)
    if entry is None:
        return err(f"角色库里找不到「{cid}」")
    if not plugin._store:
        return err("插件未初始化完成")
    # 试画台临时参数（v0.4.0）：override 存在时按其生成生效条目（不改原条目）
    override = body.get("override") if isinstance(body.get("override"), dict) else None
    if override:
        entry = draw.effective_entry(entry, override)
        if entry["strategy_type"] == "img2img":
            # 主参考或参考图库任一**真实可用**即可（v0.7.2：补一张带工作流
            # override 的 img2img 条目走 refs[] 随机；v0.9.9：与出图同口径，
            # 相对 data 目录的路径/失效文件不能再混过这道检查）
            st_now = entry.get("strategy") or {}
            # v0.11.0：多图源（gen_refs / refs）与旧单图 ref 都算显式指定
            if not draw.has_gen_refs({"strategy": st_now}, plugin._data_dir):
                return err("请先指定本次图生图源：选一张或多张参考图/立绘，或填 URL / 本地路径（不会自动取参考图列表里的图）")
    # 预览确认过的提示词直通（v0.5.9）：非空时不再走 prompt_gen，按原文出图
    prompt_override = str(body.get("prompt") or "").strip()[:2000]
    # v0.21.0：确认提示词直通时前端同步带来本轮变化维度（预览面板展示的那份，
    # 与提示词同源）——manual 路径没有维度就无法随档、详情弹窗看不到维度
    dims_override = str(body.get("dims") or "").strip()[:2000]
    # v0.15.0：主题 id 随任务记录（未确认提示词时也参与生成），归档写进立绘行
    theme_id = str(body.get("theme") or "").strip()
    job_id = f"{kind}:{cid}:{int(time.time() * 1000)}"
    plugin._jobs[job_id] = {"status": "running", "kind": kind, "char_id": cid,
                            "entry_label": pool.entry_label(entry), "src_path": "",
                            "theme": theme_id}
    _prune_jobs(plugin)
    loop = asyncio.get_running_loop()
    loop.create_task(_run_job(plugin, job_id, entry, kind, prompt_override,
                              no_archive, theme_id, dims_override))
    return ok({"job_id": job_id, "label": pool.entry_label(entry)})


def _prune_jobs(plugin) -> None:
    jobs = plugin._jobs
    if len(jobs) <= _JOB_KEEP:
        return
    for jid in sorted(jobs, key=lambda k: k.rsplit(":", 1)[-1])[: max(0, len(jobs) - _JOB_KEEP)]:
        if jobs[jid].get("status") != "running":
            jobs.pop(jid, None)


async def _run_job(plugin, job_id: str, entry: dict, kind: str,
                   prompt_override: str = "", no_archive: bool = False,
                   theme_id: str = "", dims_override: str = "") -> None:
    job = plugin._jobs.get(job_id, {})
    char_id = entry["id"]
    date = plugin._today()
    salt = str(plugin._cfg("salt", "moe-waifu"))
    # v0.15.0：seq 改单调「历史最大 + 1」，修「删过立绘后编号重复、种子撞车」
    seq = plugin._store.alloc_portrait_seq(char_id)
    seed_int = pool.portrait_seed_int(char_id, date, seq, salt)
    draw_cfg = plugin._draw_cfg()
    # v0.20.0：「random」= 从内容主题池按种子实摇（预览与生成同口径），
    # 实摇中的主题 id 随档记录——详情弹窗能看到「随机到底摇出了什么主题」
    theme_res, theme_rec = prompt_gen.resolve_theme(theme_id, seed_int)
    if prompt_override:
        # 试画台「确认提示词」直通（v0.5.9）：预览面板确认的文本按原文出图
        prompt_text, prompt_src = prompt_override, "manual"
    else:
        prompt_text, prompt_src = await prompt_gen.build_generation_prompt(
            plugin.context, draw_cfg, entry, seed_int, theme_res or ""
        )
    logger.info(f"[{PLUGIN_NAME}] 立绘提示词（{prompt_src}）：{prompt_text[:80]}")
    t0 = time.monotonic()
    src, status, error, _dur = await draw.generate_portrait(
        plugin.context, draw_cfg, entry, seed_int, _synthetic_event(),
        prompt=prompt_text,
    )
    duration = int((time.monotonic() - t0) * 1000)
    # v0.20.0：变化维度 / 图片尺寸随档记录。
    # v0.21.0：manual（确认提示词直通）路径改用前端随 prompt 带来的维度
    # （预览面板展示的那份，含 attempt/dim_state 影响，比后端复算更准确）
    dims_json = ""
    if prompt_src != "manual":
        dims_json = json.dumps(prompt_gen.describe_variations(
            seed_int, theme_rec,
            force_single=(str(entry.get("strategy_type") or "") == "img2img")),
            ensure_ascii=False)
    elif dims_override:
        dims_json = dims_override
    img_w, img_h = draw.image_size(src) if src else (0, 0)
    plugin._store.log_job(char_id, date, "try" if kind == "try" else "refill", status,
                          "workshop", entry["strategy_type"], seed_int, error, duration, src or "")
    job.update({"status": status, "error": error, "duration_ms": duration,
                "seed": seed_int, "src_path": src or "", "char_id": char_id,
                "prompt": prompt_text[:500], "prompt_src": prompt_src, "theme": theme_rec,
                "dims": dims_json, "width": img_w, "height": img_h})
    if src and kind == "refill" and not no_archive:
        rel = draw.archive_portrait(src, plugin._data_dir, char_id, date, seq, seed_int)
        plugin._store.add_portrait(char_id, rel, seed_int, date, seq,
                                   entry["strategy_type"], prompt=prompt_text, theme=theme_rec,
                                   dims=dims_json, width=img_w, height=img_h)
        job["archived"] = rel
    if src:
        # 试画缓存（v0.5.9）：萌绘产物在其自己的数据目录里，/image/data 只认本插件
        # 数据目录——大图查看前先把成品拷进 try_cache/，src_path 指向这份拷贝
        # （归档/add_portrait 都是再拷贝语义，不受影响）。7 天后由启动清理回收。
        try:
            cache_dir = Path(plugin._data_dir) / "try_cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            cached = cache_dir / f"{job_id.replace(':', '_')}.png"
            shutil.copy2(src, cached)
            job["src_path"] = str(cached)
        except Exception:
            logger.warning(f"[{PLUGIN_NAME}] 试画缓存拷贝失败（大图查看将不可用）\n"
                           f"{traceback.format_exc()}")
    if job.get("src_path"):
        job["preview"] = _thumb_data_url(job["src_path"], 420)
    plugin._jobs[job_id] = job


async def h_job_status(plugin) -> dict:
    body = await _body()
    jid = str(body.get("job_id") or _q("job_id", "") or "").strip()
    job = plugin._jobs.get(jid)
    if job is None:
        return err("job 不存在（可能已过期）")
    out = {k: v for k, v in job.items()}
    return ok(out)


async def h_job_archive(plugin) -> dict:
    body = await _body()
    jid = str(body.get("job_id") or "").strip()
    job = plugin._jobs.get(jid)
    if job is None or job.get("status") != "ok" or not job.get("src_path"):
        return err("job 不存在或未成功")
    if job.get("archived"):
        return ok({"archived": job["archived"]})
    if not plugin._store:
        return err("插件未初始化完成")
    cid = job["char_id"]
    date = plugin._today()
    seq = plugin._store.alloc_portrait_seq(cid)
    rel = draw.archive_portrait(job["src_path"], plugin._data_dir, cid, date, seq,
                                int(job.get("seed") or 0))
    plugin._store.add_portrait(cid, rel, int(job.get("seed") or 0), date, seq, "try",
                               prompt=str(job.get("prompt") or "") or None,
                               theme=str(job.get("theme") or ""),
                               dims=str(job.get("dims") or ""),
                               width=int(job.get("width") or 0),
                               height=int(job.get("height") or 0))
    job["archived"] = rel
    return ok({"archived": rel})


async def h_job_to_ref(plugin) -> dict:
    """把已成功 job 的成品图转存为该角色的参考图（不进立绘库，v0.7.5）。

    文件拷到 data/refs/gen_<时间戳>_<角色>.png 并绑进 strategy.refs（去重追加）。
    同一 job 只拷一次文件，重复调用只补绑定（幂等）；与归档互不影响。
    """
    body = await _body()
    jid = str(body.get("job_id") or "").strip()
    job = plugin._jobs.get(jid)
    if job is None or job.get("status") != "ok" or not job.get("src_path"):
        return err("job 不存在或未成功")
    cid = str(job.get("char_id") or body.get("id") or "").strip()
    if not cid:
        return err("job 缺角色 id，无法绑定")
    rel = str(job.get("to_ref") or "")
    if not rel:
        src = Path(job["src_path"])
        if not src.is_file():
            return err("成品文件已不存在（try_cache 已过期回收）")
        refs_dir = Path(plugin._data_dir) / "refs"
        refs_dir.mkdir(parents=True, exist_ok=True)
        safe_cid = re.sub(r"[^0-9A-Za-z_-]", "_", cid)[:30] or "char"
        fname = f"gen_{int(time.time())}_{safe_cid}{(src.suffix or '.png').lower()}"
        shutil.copy2(src, refs_dir / fname)
        rel = f"refs/{fname}"
        job["to_ref"] = rel

    def mutate(e, raw):
        st = raw.get("strategy") if isinstance(raw.get("strategy"), dict) else {}
        refs = [str(x) for x in (st.get("refs") if isinstance(st.get("refs"), list) else [])]
        if rel not in refs:
            refs.append(rel)
        st["refs"] = refs
        raw["strategy"] = st

    pack_name = _set_entry_field(plugin, cid, mutate)
    if pack_name is None:
        return err(f"角色库里找不到 {cid}")
    # v0.20.0：生成信息随参考图记录（详情弹窗查看；同一文件重复转存时覆盖）
    if plugin._store:
        try:
            plugin._store.set_ref_meta(
                rel, prompt=str(job.get("prompt") or "") or None,
                theme=str(job.get("theme") or ""),
                seed=(int(job.get("seed")) if job.get("seed") else None),
                source="try",
                width=int(job.get("width") or 0), height=int(job.get("height") or 0))
        except Exception:
            logger.warning(f"[{PLUGIN_NAME}] 参考图生成信息记录失败（不影响绑定）\n"
                           f"{traceback.format_exc()}")
    return ok({"rel": rel, "path": str(Path(plugin._data_dir) / rel)})


# ---------- 分群绑定 ----------

async def h_group_pools_get(plugin) -> dict:
    return ok({"text": str(plugin._cfg("group_pools", "") or "")})


async def h_group_pools_set(plugin) -> dict:
    body = await _body()
    text = str(body.get("text") or "")
    parsed = pool.parse_group_pools(text)
    cfg = getattr(plugin, "config", None)
    if cfg is None or not callable(getattr(cfg, "save_config", None)):
        return err("插件配置不可写（self.config 缺失）")
    cfg["group_pools"] = text
    cfg.save_config()
    return ok({"groups": parsed})


# ---------- 作品库（按 series 聚合） ----------

async def h_works(plugin) -> dict:
    """按条目 series 聚合作品。封面优先级（v0.5.0）：works_meta.json 手动指定 →
    角色条目 cover → 第一张立绘。没有 series 的条目不进作品库（角色库仍可见）。
    """
    if not plugin._store:
        return err("插件未初始化完成")
    works_meta = _load_works_meta(plugin)
    work_map: dict = {}
    for pack in pool.load_packs(_pools_dir(plugin), logger):
        for raw in pack["entries"]:
            e = pool.normalize_entry(raw, pack["pack"])
            if e is None or not e["series"]:
                continue
            w = work_map.setdefault(e["series"], {
                "series": e["series"], "category": e["category"],
                "color": e["color"], "total": 0, "verified": 0,
                "portrait_count": 0, "characters": [],
            })
            w["total"] += 1
            if e["verified"]:
                w["verified"] += 1
            rows = plugin._store.list_portraits(e["id"])
            n = len(rows)
            w["portrait_count"] += n
            char_cover = _char_cover_path(plugin, e)
            w["characters"].append({
                "id": e["id"], "name": e["name"], "tier": e["tier"],
                "color": e["color"], "verified": e["verified"], "portraits": n,
                "thumb": _thumb_cached(char_cover, 420) if char_cover else None,
                "cover_path": char_cover,   # 作品封面候选（v0.5.0）
            })
    works = []
    for w in work_map.values():
        w["characters"].sort(key=lambda c: (not c["verified"], c["id"]))
        cover = None
        meta_cover = str((works_meta.get(w["series"]) or {}).get("cover") or "")
        if meta_cover:
            fp = _safe_data_file(plugin, meta_cover)
            if fp is not None:
                cover = _thumb_cached(fp, 420)
        if cover is None:
            for c in w["characters"]:
                if c.get("cover_path"):
                    cover = _thumb_cached(c["cover_path"], 420)
                    if cover:
                        break
        w["cover"] = cover
        w["meta_cover"] = meta_cover
        works.append(w)
    works.sort(key=lambda w: (-w["total"], w["series"]))
    return ok({"works": works, "version": _version_of()})


async def h_set_cover(plugin) -> dict:
    """设置/清除角色封面（entry.cover）。path 传空串清除。"""
    body = await _body()
    cid = str(body.get("id") or "").strip()
    cover = str(body.get("path") or "").strip()
    if not cid:
        return err("缺少 id")
    if cover and _safe_data_file(plugin, cover) is None:
        return err("封面文件不存在或不在数据目录内")
    pack_name = _set_entry_field(
        plugin, cid,
        lambda e, raw: (raw.__setitem__("cover", cover) if cover
                        else raw.pop("cover", None)),
    )
    if pack_name is None:
        return err(f"角色库里找不到 {cid}")
    _THUMB_CACHE.clear()  # 封面变了，缩略缓存整体失效（键含路径，旧的不会命中，保险起见清掉）
    return ok({"id": cid, "cover": cover})


async def h_works_set_cover(plugin) -> dict:
    """设置/清除作品封面（works_meta.json）。path 传空串清除。"""
    body = await _body()
    series = str(body.get("series") or "").strip()
    cover = str(body.get("path") or "").strip()
    if not series:
        return err("缺少 series")
    if cover and _safe_data_file(plugin, cover) is None:
        return err("封面文件不存在或不在数据目录内")
    meta = _load_works_meta(plugin)
    slot = meta.get(series) if isinstance(meta.get(series), dict) else {}
    if cover:
        slot["cover"] = cover
        meta[series] = slot
    else:
        meta.pop(series, None)
    _save_works_meta(plugin, meta)
    _THUMB_CACHE.clear()   # 与 h_set_cover 对称：作品封面变了，缩略整体失效
    return ok({"series": series, "cover": cover})


async def h_refs_list(plugin) -> dict:
    """某角色的参考图库：strategy.refs 列表 + 主参考（strategy.ref）。"""
    body = await _body()
    cid = str(body.get("id") or _q("id", "") or "").strip()
    if not cid:
        return err("缺少 id")
    entry = pool.find_entry(pool.load_packs(_pools_dir(plugin), logger), cid)
    if entry is None:
        return err(f"角色库里找不到「{cid}」")
    st = entry.get("strategy") or {}
    refs = st.get("refs") if isinstance(st.get("refs"), list) else []
    items = []
    for p in refs:
        p = str(p or "").strip()
        if not p:
            continue
        fp = _safe_data_file(plugin, p)
        # 缩略 420（v0.6.2）：旧值 120 在参考图墙上放大约 2.5 倍，糊到没法看
        items.append({
            "path": p, "exists": fp is not None,
            "thumb": _thumb_cached(fp, 420) if fp is not None else None,
        })
    # v0.20.0：生成信息随图返回（试画转入 / 立绘转入时有，手动上传为 null）
    metas = {}
    if plugin._store and items:
        try:
            metas = plugin._store.get_ref_meta([it["path"] for it in items])
        except Exception:
            metas = {}
    for it in items:
        m = metas.get(it["path"])
        it["meta"] = dict(m) if m else None
    # draw_ref 存的是归一后的相对路径；立绘卡用绝对路径比较，所以再给一份
    # 绝对路径（v0.12.0：否则「设为出卡源」的立绘卡片不会高亮）。
    draw_raw = str(st.get("draw_ref") or "")
    draw_abs = ""
    if draw_raw:
        if re.match(r"^[a-zA-Z]+://", draw_raw):
            draw_abs = draw_raw
        else:
            fp = _safe_data_file(plugin, draw_raw)
            draw_abs = str(fp) if fp is not None else ""
    return ok({"id": cid, "items": items,
               "designated": str(st.get("ref") or ""),
               "draw_ref": draw_raw, "draw_ref_abs": draw_abs})


async def h_refs_set_draw_source(plugin) -> dict:
    """设置 / 清空角色的「出卡图生图源」（strategy.draw_ref）。

    与参考图库、主参考完全独立：只有这里指定的一张能用于 `/今日老婆`
    的图生图；path 传空串 = 取消指定（之后抽卡不再走图生图）。
    """
    body = await _body()
    cid = str(body.get("id") or "").strip()
    path = str(body.get("path") or "").strip()
    if not cid:
        return err("缺少 id")
    norm = ""
    if path:
        if re.match(r"^[a-zA-Z]+://", path):
            norm = path
        else:
            if _safe_data_file(plugin, path) is None:
                return err("出卡图生图源文件不存在或不在数据目录内")
            norm = _norm_ref(plugin, path)
            if not norm:
                return err("路径无法归一化")

    def mutate(e, raw):
        st = raw.get("strategy") if isinstance(raw.get("strategy"), dict) else {}
        st["draw_ref"] = norm
        raw["strategy"] = st

    if _set_entry_field(plugin, cid, mutate) is None:
        return err(f"角色库里找不到 {cid}")
    return ok({"id": cid, "draw_ref": norm})


def _orphan_items(plugin) -> list:
    """data/refs/ 下没被任何角色 / 作品封面 / 出卡源引用的图片（找回用）。

    v0.9.10 之前「编辑角色点保存」会静默清空 strategy.refs，文件其实还在
    磁盘上，只是没有任何列表能找到它们。不扫描 data 目录以外，不改任何文件。
    """
    refs_dir = Path(plugin._data_dir) / "refs"
    if not refs_dir.is_dir():
        return []
    used = set()

    def probe(raw):
        st = raw.get("strategy") if isinstance(raw.get("strategy"), dict) else {}
        vals = [st.get("ref"), st.get("draw_ref"), raw.get("cover")]
        vals += list(st.get("refs") or []) if isinstance(st.get("refs"), list) else []
        for v in vals:
            n = _norm_ref(plugin, v)
            if n:
                used.add(n)
        return False

    _walk_pack_entries(plugin, probe)
    for slot in _load_works_meta(plugin).values():
        if isinstance(slot, dict):
            n = _norm_ref(plugin, slot.get("cover"))
            if n:
                used.add(n)
    items = []
    for f in sorted(refs_dir.iterdir()):
        if not f.is_file() or f.suffix.lower() not in (
                ".png", ".jpg", ".jpeg", ".webp", ".gif"):
            continue
        rel = f"refs/{f.name}"
        if rel in used:
            continue
        try:
            items.append({"path": rel, "thumb": _thumb_cached(f, 240),
                          "size": f.stat().st_size})
        except Exception:
            items.append({"path": rel, "thumb": None, "size": 0})
    return items


async def h_refs_orphans(plugin) -> dict:
    """列出未绑定图片，工坊可一键重新绑定到当前角色。"""
    return ok({"items": await asyncio.to_thread(_orphan_items, plugin)})


async def h_refs_orphan_delete(plugin) -> dict:
    """物理删除一张**未绑定**的参考图（data/refs/ 下的孤儿文件）。

    安全约束与 /refs/orphans 同一套：路径必须落在 refs/ 内，且当前没有任何
    角色条目 / 作品封面 / 出卡源引用它；仍被引用时拒绝，避免「删孤儿把别人
    还在用的图删了」。真正的删除语义只有用户在这里点两段确认才会发生。
    """
    body = await _body()
    path = str(body.get("path") or "").strip()
    if not path:
        return err("缺少 path")
    fp = _safe_data_file(plugin, path)
    if fp is None:
        return err("文件不存在或不在数据目录内")
    norm = _norm_ref(plugin, path)
    if not norm.startswith("refs/"):
        return err("只能删除 refs/ 下的未绑定参考图")
    if _path_is_referenced(plugin, norm):
        return err("这张图仍被某个角色或作品封面引用，请先解绑再删除")
    try:
        await asyncio.to_thread(fp.unlink)
    except FileNotFoundError:
        return ok({"deleted": path, "already_gone": True})
    except Exception as exc:
        logger.warning(f"[{PLUGIN_NAME}] 删除未绑定图片失败\n{traceback.format_exc()}")
        return err(f"删除失败：{exc}")
    logger.info(f"[{PLUGIN_NAME}] 已物理删除未绑定图片：{norm}")
    return ok({"deleted": path})


async def h_refs_bind(plugin) -> dict:
    """把一张已存在的图片绑进角色的参考图库（strategy.refs，去重追加）。"""
    body = await _body()
    cid = str(body.get("id") or "").strip()
    path = str(body.get("path") or "").strip()
    if not cid or not path:
        return err("缺少 id 或 path")
    if _safe_data_file(plugin, path) is None and not re.match(r"^[a-zA-Z]+://", path):
        return err("文件不存在或不在数据目录内")

    def mutate(e, raw):
        st = raw.get("strategy") if isinstance(raw.get("strategy"), dict) else {}
        refs = [str(x) for x in (st.get("refs") if isinstance(st.get("refs"), list) else [])]
        if path not in refs:
            refs.append(path)
        st["refs"] = refs
        raw["strategy"] = st

    pack_name = _set_entry_field(plugin, cid, mutate)
    if pack_name is None:
        return err(f"角色库里找不到 {cid}")
    return ok({"id": cid, "bound": path})


async def h_refs_unbind(plugin) -> dict:
    """从参考图库移除一张（可选连带删除文件）；若是主参考一并清空。"""
    body = await _body()
    cid = str(body.get("id") or "").strip()
    path = str(body.get("path") or "").strip()
    delete_file = bool(body.get("delete_file"))
    if not cid or not path:
        return err("缺少 id 或 path")

    # 归一后比较：refs 里同一张图可能同时存着绝对路径与 `refs/xxx.png`
    norm = _norm_ref(plugin, path)

    def mutate(e, raw):
        st = raw.get("strategy") if isinstance(raw.get("strategy"), dict) else {}
        refs = [str(x) for x in (st.get("refs") or []) if _norm_ref(plugin, x) != norm]
        st["refs"] = refs
        if _norm_ref(plugin, st.get("ref")) == norm:
            st["ref"] = ""
        if _norm_ref(plugin, st.get("draw_ref")) == norm:
            st["draw_ref"] = ""   # 出卡源指向这张图时一并取消，避免死链
        raw["strategy"] = st

    pack_name = _set_entry_field(plugin, cid, mutate)
    if pack_name is None:
        return err(f"角色库里找不到 {cid}")
    removed_file = False
    if delete_file:
        fp = _safe_data_file(plugin, path)
        # 同一张 refs 可能被多个角色 / 作品封面共用：解绑当前角色后还有引用
        # 就绝不能物理删文件，否则别的角色参考图会凭空消失（v0.9.10 修复）。
        if fp is not None and not _path_is_referenced(plugin, norm):
            try:
                fp.unlink()
                removed_file = True
            except Exception:
                pass
    return ok({"id": cid, "removed": path, "file_deleted": removed_file})


async def h_image_data(plugin) -> dict:
    """按路径返回整图 data URL（工坊点击放大查看用，走桥的 apiGet 通道）。

    路径经 _safe_data_file 校验（必须落在 data 目录内）。
    """
    body = await _body()
    path = str(body.get("path") or _q("path", "") or "").strip()
    fp = _safe_data_file(plugin, path)
    if fp is None:
        return err("图片不存在或不在数据目录内")
    if fp.stat().st_size > 25 * 1024 * 1024:
        return err("图片超过 25MB，无法预览")
    import base64 as _b64
    import mimetypes as _mimetypes
    mime = _mimetypes.guess_type(str(fp))[0] or "image/png"
    data_url = "data:" + mime + ";base64," + _b64.b64encode(fp.read_bytes()).decode("ascii")
    return ok({"data_url": data_url, "name": fp.name})


async def h_llm_providers(plugin) -> dict:
    """列出可用的对话模型（LLM 提供商），供配置页「提示词用对话模型」下拉。

    provider 元数据读 provider_config（id / model_config.model）；单个实例
    读取失败只跳过该实例，不影响整体。
    """
    items = []
    seen: set = set()
    try:
        provs = plugin.context.get_all_providers() or []
    except Exception:
        provs = []
    for p in provs:
        try:
            conf = getattr(p, "provider_config", None) or {}
            pid = str(conf.get("id") or getattr(p, "id", "") or "").strip()
            if not pid or pid in seen:
                continue
            seen.add(pid)
            model = ""
            try:
                model = str(p.get_model() or "")
            except Exception:
                model = ""
            if not model:
                mc = conf.get("model_config") or {}
                model = str(mc.get("model") or "") if isinstance(mc, dict) else ""
            items.append({"id": pid, "model": model})
        except Exception:
            continue
    items.sort(key=lambda it: it["id"])
    return ok({"providers": items})


# ---------- 主题与提示词预览（试画台第 2 步，v0.6.0） ----------

async def h_themes(plugin) -> dict:
    return ok({"themes": [{"id": t["id"], "label": t["label"]}
                          for t in prompt_gen.THEMES]})


async def h_prompt_preview(plugin) -> dict:
    """生成提示词预览（不绘图）：返回维度标签 + 最终提示词文本。

    attempt 用于「换一版」：混进种子做预览层扰动（不影响抽卡主链路的决定论）。
    dim_state（v0.14.0）：维度级控制 {attempts, fixed, disabled, enabled}——
    面板每个维度 chip 可单独重摇/锁定/移除/启用，见 prompt_gen.apply_dim_state。
    用户确认后的提示词文本经 jobs/start 的 prompt 字段直通出图。
    响应 dims 字段保持旧形状（非空维度 [{label, zh}]，试画台只读展示用），
    全量维度状态另放 dim_control（生成立绘弹窗的可交互 chip 用）。
    """
    body = await _body()
    cid = str(body.get("id") or "").strip()
    if not cid:
        return err("缺少 id")
    entry = pool.find_entry(pool.load_packs(_pools_dir(plugin), logger), cid)
    if entry is None:
        return err(f"角色库里找不到「{cid}」")
    override = body.get("override") if isinstance(body.get("override"), dict) else None
    if override:
        entry = draw.effective_entry(entry, override)
    theme_id = str(body.get("theme") or "random").strip()
    custom = str(body.get("custom") or "").strip()[:300]
    outfit = str(body.get("outfit") or "").strip().strip("。，,")[:80]
    try:
        attempt = max(0, int(body.get("attempt") or 0))
    except (TypeError, ValueError):
        attempt = 0
    dim_state = body.get("dim_state") if isinstance(body.get("dim_state"), dict) else {}
    # 穿搭输入框非空时 outfit 由 override 接管（pick_variations 末段覆盖），
    # 维度级重摇/锁定对 outfit 不生效——避免「面板锁了款、输入框又盖掉」的歧义
    if outfit:
        dim_state = dict(dim_state)
        fixed = dict(dim_state.get("fixed") or {})
        fixed.pop("outfit", None)
        attempts = dict(dim_state.get("attempts") or {})
        attempts.pop("outfit", None)
        dim_state["fixed"] = fixed
        dim_state["attempts"] = attempts
        dim_state["disabled"] = [d for d in (dim_state.get("disabled") or [])
                                 if d != "outfit"]
        dim_state["enabled"] = [d for d in (dim_state.get("enabled") or [])
                                if d != "outfit"]
    date = plugin._today()
    salt = str(plugin._cfg("salt", "moe-waifu"))
    # v0.23.1：预览只读查询「下一个可用 seq」，绝不消耗号；实际生成走
    # alloc_portrait_seq() 原子占用，正常情况下预览编号 = 实出编号。
    seq = plugin._store.next_portrait_seq(cid) if plugin._store else 0
    seed_int = pool.portrait_seed_int(cid, date, seq, salt)
    if attempt:
        seed_int = (seed_int + attempt * 7919) % (2 ** 31)
    cfg = plugin._draw_cfg()
    prompt_text, source = await prompt_gen.build_generation_prompt(
        plugin.context, cfg, entry, seed_int, theme_id, custom, outfit, dim_state
    )
    # dims 展示与提示词文本必须同源同序：镜像 build_generation_prompt 内部的
    # picks 计算链（全池→主题子集→图生图强制单图→维度级控制），两处改动要同步。
    # v0.20.0：random 在预览与生成两侧都按同一种子实摇（resolve_theme 决定论）
    theme, _theme_rec = prompt_gen.resolve_theme(theme_id, seed_int)
    picks = prompt_gen.pick_variations(seed_int, theme, outfit)
    # v0.22.4：与 build_generation_prompt 同口径——非模板主题一律强制单图，
    # 不再按 strategy_type 区分（文生图此前会漏，预览显示三视图、实际出单图）。
    if not (theme and theme.get("compose_kw")):
        picks = prompt_gen.force_single_view(picks)
    picks = prompt_gen.apply_dim_state(picks, seed_int, dim_state, theme)
    dims = [{"label": label, "zh": zh}
            for label, zh in prompt_gen.variation_zh_list(picks)]
    body_zh = str((picks.get("body") or {}).get("zh") or "").strip()
    style = ""
    if str(entry.get("strategy_type") or "base") != "img2img":
        style = prompt_gen.detect_t2i_style(plugin.context, cfg.get("draw_workflow", ""))
    # v0.14.0：全量维度状态（生成立绘弹窗 chip 渲染 + 增删维度菜单用）
    fixed_map = dim_state.get("fixed") if isinstance(dim_state.get("fixed"), dict) else {}
    disabled = set(dim_state.get("disabled") or [])
    dim_control = {
        "dims": [{"dim": tag, "label": label,
                  "zh": str((picks.get(tag) or {}).get("zh") or "").strip(),
                  "fixed": tag in fixed_map, "off": tag in disabled}
                 for label, tag in prompt_gen.DIM_ORDER],
        "seed": seed_int, "attempt": attempt,
    }
    return ok({"prompt": prompt_text, "source": source, "style": style,
               "dims": dims, "body": body_zh, "seed": seed_int, "attempt": attempt,
               "dim_control": dim_control})


async def h_prompt_pools(plugin) -> dict:
    """维度池下拉选项（v0.14.0）：每个维度池的非空中文短语，按权重降序。

    生成立绘弹窗打开时拉一次即可（池是静态数据），供「点击 chip 选定值 /
    单独随机」用；不含配色（配色跟随种子随机，锁定款式时不叠色）。
    """
    return ok({"pools": prompt_gen.dim_pool_options()})


# ---------- 作品发现（v0.18.0）：作品名 → websearch(可选增强) + LLM → 角色草稿 → 勾选入库 ----------

_DISCOVER_SYSTEM = (
    "你为「萌萌今日老婆」插件的角色库草稿补充角色。只输出一个 JSON 数组——不要 "
    "markdown 围栏、不要解释、不要多余文本。每项字段：name（角色中文常用译名）、"
    "series（作品名）、category（anime / gal / game / other 之一）、tier（S/A/B/C，"
    "人气越高越高档，宁低勿高）、tags_en（英文 danbooru 风格画图 tags，逗号分隔，"
    "必须包含发色、瞳色、发型、显著服饰或特征）、color（贴合角色形象的 #RRGGBB "
    "主题色）。只收录女性角色（本插件抽卡对象为女角色）；5~12 位；拿不准的角色"
    "宁缺勿编。"
)


def _extract_json_array(text: str) -> list:
    """从 LLM 输出里抠第一个平衡的 JSON 数组（容错围栏/前后废话/转义引号）。"""
    s = str(text or "")
    start = s.find("[")
    if start < 0:
        return []
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                import json as _json
                try:
                    arr = _json.loads(s[start:i + 1])
                    return arr if isinstance(arr, list) else []
                except Exception:
                    return []
    return []


def _ws_run_context(plugin):
    """给内部 websearch 工具的 call(context) 造最小 ContextWrapper 桩。

    TavilyWebSearchTool.call 等只经 _get_runtime(context) 读
    context.context.event.unified_msg_origin 与 context.context.context.get_config(umo)
    （拿 provider_settings 里的 API key），三个属性齐即可。
    """
    import types as _types
    try:
        cfg = plugin.context.get_config()
    except Exception:
        cfg = {}
    event = _types.SimpleNamespace(unified_msg_origin="webui")
    inner = _types.SimpleNamespace(get_config=lambda umo=None: cfg)
    agent_ctx = _types.SimpleNamespace(event=event, context=inner)
    return _types.SimpleNamespace(context=agent_ctx)


async def _discover_search_text(plugin, series: str) -> str:
    """内部 websearch 工具链（按常用顺序探测第一个可用且配置了 key 的）。

    失败/未配置/超时一律返回空串（降级 LLM-only，作品发现不因此不可用）。
    """
    names = ("web_search_tavily", "web_search_bocha", "web_search_brave",
             "web_search_baidu", "web_search_exa", "web_search_anysearch",
             "web_search_serpapi", "web_search_deepseek")
    try:
        manager = plugin.context.get_llm_tool_manager()
        if manager is None:
            return ""
        run_ctx = _ws_run_context(plugin)
        for name in names:
            try:
                tool = manager.get_tool(name)
            except Exception:
                tool = None
            if tool is None or not bool(getattr(tool, "active", True)):
                continue
            try:
                res = await asyncio.wait_for(
                    tool.call(run_ctx, query=series + " 角色 成员 列表 资料",
                              max_results=10),
                    timeout=25)
            except Exception:
                continue
            text = res if isinstance(res, str) else str(res)
            if text and not text.startswith("Error"):
                return text[:6000]
    except Exception:
        logger.warning("[" + PLUGIN_NAME + "] 作品发现 websearch 不可用，降级 LLM-only",
                       exc_info=True)
    return ""


async def _discover_llm_draft(plugin, series: str, search_text: str) -> list:
    """LLM 生成角色草稿（复用提示词 LLM 通道）；失败返回空表。"""
    try:
        cfg = plugin._draw_cfg()
        provider = await prompt_gen._resolve_provider(
            plugin.context, cfg.get("prompt_llm_provider", ""))
        if provider is None:
            return []
        timeout = int(cfg.get("prompt_llm_timeout", 30) or 30)
        if timeout <= 0:
            timeout = 30
        segs = ["作品：" + series + "。请给出该作品中适合抽取的女性角色草稿。"]
        if search_text:
            segs.append("网络检索摘要（仅供参考，可能截断或失效）：\n" + search_text)
        resp = await asyncio.wait_for(
            provider.text_chat(prompt="\n".join(segs), session_id=None,
                               system_prompt=_DISCOVER_SYSTEM),
            timeout=timeout + 30)
        arr = _extract_json_array(getattr(resp, "completion_text", "") or "")
        out = []
        for it in arr if isinstance(arr, list) else []:
            if not isinstance(it, dict):
                continue
            name = str(it.get("name") or "").strip()
            tags = str(it.get("tags_en") or "").strip()
            if not name or not tags:
                continue
            out.append({
                "id": "disc_" + str(abs(hash((series, name))) % 10 ** 8),
                "name": name[:40],
                "series": str(it.get("series") or series).strip()[:60],
                "category": str(it.get("category") or "anime").strip().lower(),
                "tier": str(it.get("tier") or "C").strip().upper()[:2],
                "tags_en": tags[:300],
                "color": str(it.get("color") or "#E8B4C8").strip()[:9],
            })
        return out[:12]
    except Exception:
        logger.warning("[" + PLUGIN_NAME + "] 作品发现 LLM 草稿失败", exc_info=True)
        return []


async def h_works_discover(plugin) -> dict:
    """作品发现：作品名 → 角色草稿（不入库）。source 标注 llm / websearch+llm。"""
    body = await _body()
    series = str(body.get("series") or "").strip()[:60]
    if not series:
        return err("请输入作品名")
    search_text = await _discover_search_text(plugin, series)
    candidates = await _discover_llm_draft(plugin, series, search_text)
    if not candidates:
        return err("没有生成候选：检查对话模型是否可用，或换一个作品名")
    return ok({"series": series, "candidates": candidates,
               "source": "websearch+llm" if search_text else "llm"})


def _discover_add_core(plugin, pack_name: str, rows: list) -> dict:
    """勾选草稿批量入库（同步 core，脱离 quart 可直测）。"""
    loaded = _load_pack_file(plugin, pack_name)
    if loaded is None:
        return {"error": "卷不存在：" + pack_name}
    path, data = loaded
    entries = data.get("entries") if isinstance(data.get("entries"), list) else []
    existing_ids = set()
    for pack in pool.load_packs(_pools_dir(plugin), logger):
        for raw in pack["entries"]:
            other = pool.normalize_entry(raw, pack["pack"])
            if other:
                existing_ids.add(other["id"])
    added, dup = 0, 0
    for row in rows[:12]:
        if not isinstance(row, dict):
            continue
        normalized = pool.normalize_entry(row, pack_name)
        if normalized is None:
            continue
        if normalized["id"] in existing_ids:
            dup += 1
            continue
        entries.append(normalized)
        existing_ids.add(normalized["id"])
        added += 1
    if added:
        data["entries"] = entries
        _json_save(path, data)
    return {"added": added, "dup": dup}


async def h_works_discover_add(plugin) -> dict:
    """勾选的草稿批量入库（unverified，入库后去试画验证）。"""
    body = await _body()
    pack_name = str(body.get("pack") or "").strip()
    rows = body.get("entries")
    if not pack_name or not isinstance(rows, list) or not rows:
        return err("缺少 pack 或 entries")
    result = _discover_add_core(plugin, pack_name, rows)
    if "error" in result:
        return err(result["error"])
    return ok(result)


# ---------- 批量补图队列（v0.16.0） ----------
# 前提（用户口径）：① 角色有可用图生图源（strategy.ref / gen_refs 文件真实存在，
# 「不存在则跳过」；batch_require_ref=false 可关）② 立绘数不足配额（缺几张补几张）。
# 主题由 batch_fill.theme_plan 伪随机轮换（相邻不重复），随档记录。

def _batch_runner(plugin):
    """插件实例常驻的队列（进程内存态，重启即清）；首次访问惰性创建。"""
    runner = getattr(plugin, "_batch", None)
    if runner is None:
        runner = batch_fill.BatchRunner()
        plugin._batch = runner
    return runner


def _batch_scope_entries(plugin, body) -> list:
    """范围筛选：启用卷 ∩ verified，再按 mode=all/packs/series/chars 过滤。"""
    packs = pool.load_packs(_pools_dir(plugin), logger)
    mode = str(body.get("mode") or "all").strip()
    sel_packs = {str(x) for x in (body.get("packs") or [])}
    sel_series = {str(x) for x in (body.get("series") or [])}
    sel_chars = {str(x) for x in (body.get("chars") or [])}
    out = []
    for p in packs:
        if not p.get("enabled", True):
            continue
        for e in p.get("entries") or []:
            if not e.get("verified", False):
                continue
            if mode == "packs" and (not sel_packs or str(e.get("pack") or "") not in sel_packs):
                continue
            if mode == "series" and (not sel_series or str(e.get("series") or "") not in sel_series):
                continue
            if mode == "chars" and (not sel_chars or str(e.get("id") or "") not in sel_chars):
                continue
            out.append(e)
    return out


def _batch_candidates(plugin, body) -> tuple:
    """按前提筛出补图对象，返回 (candidates, skipped_ref, quota, require_ref)。"""
    entries = _batch_scope_entries(plugin, body)
    quota = max(1, int(plugin._cfg("portraits_per_char", 5)))
    require_ref = bool(plugin._cfg("batch_require_ref", True))
    cands = batch_fill.pick_candidates(
        entries,
        lambda cid: (plugin._store.count_portraits(cid) if plugin._store else 0),
        quota, require_ref,
        lambda e: draw.has_gen_refs(e, plugin._data_dir))
    # 预览友好：因「无可用参考图」被跳过的角色（立绘不足配额的那部分）
    chosen = {c["entry"].get("id") for c in cands}
    skipped = []
    for e in entries:
        if e.get("id") in chosen:
            continue
        have = plugin._store.count_portraits(e.get("id")) if plugin._store else 0
        if have < quota and require_ref and not draw.has_gen_refs(e, plugin._data_dir):
            skipped.append({"char_id": e.get("id"), "name": e.get("name"),
                            "reason": "无可用参考图"})
    # v0.22.4：该角色已用过的主题一并交给 theme_plan，续补不再重放老主题。
    # 预览与实际启动共用这份 cands，主题序列才不会「预览一套、实出一套」。
    for c in cands:
        try:
            rows = (plugin._store.list_portraits(str(c["entry"].get("id") or ""))
                    if plugin._store else [])
        except Exception:
            rows = []
        used = []
        for r in rows or []:
            try:
                t = str(r["theme"] or "").strip()
            except (KeyError, IndexError, TypeError):
                t = ""
            if t:
                used.append(t)
        c["used_themes"] = used
    return cands, skipped, quota, require_ref


async def h_batch_preview(plugin) -> dict:
    """dry-run：返回将补图的角色清单（含每张将套用的主题序列）与跳过名单。"""
    body = await _body()
    cands, skipped, quota, require_ref = _batch_candidates(plugin, body)
    salt = str(plugin._cfg("salt", "moe-waifu"))
    date = plugin._today()
    labels = {t["id"]: t["label"] for t in prompt_gen.THEMES}
    items = []
    for c in cands:
        e = c["entry"]
        themes = batch_fill.theme_plan(
            str(e.get("id") or ""), salt, c["need"],
            have=c["have"], date=date, used=c.get("used_themes") or [])
        labels_list = [labels.get(t, t) for t in themes]
        items.append({
            "char_id": e.get("id"), "name": e.get("name"),
            "series": e.get("series"), "pack": e.get("pack"),
            "have": c["have"], "need": c["need"],
            "themes": labels_list,
            # v0.16.0：拼好的展示文本——⚠️ 工坊页跑在 sandbox iframe 里，
            # modal slot 内插值调用 .join()/.map() 会在数据更新时打断整块
            # 渲染（内容静默清空、无任何 console 警告；复现页实测），模板
            # 一律纯字段插值，join/map 留在后端
            "themes_text": " / ".join(labels_list),
        })
    items.sort(key=lambda x: (str(x.get("pack") or ""), str(x.get("name") or "")))
    skipped_text = "、".join(str(s.get("name") or s.get("char_id") or "")
                             for s in skipped)
    return ok({"items": items, "total": sum(c["need"] for c in cands),
               "skipped": skipped, "skipped_text": skipped_text,
               "quota": quota, "require_ref": require_ref})


async def h_batch_start(plugin) -> dict:
    """启动批量补图队列。running 时拒绝重复启动。"""
    if not plugin._store:
        return err("插件未初始化完成")
    body = await _body()
    cands, skipped, quota, require_ref = _batch_candidates(plugin, body)
    if not cands:
        return err("没有满足前提的角色：检查配额是否已满、或角色是否有可用参考图")
    salt = str(plugin._cfg("salt", "moe-waifu"))
    tasks = batch_fill.build_tasks(cands, salt, date=plugin._today())
    runner = _batch_runner(plugin)
    n = runner.start(plugin, tasks)
    if n < 0:
        return err("批量补图正在运行中，请先停止或等待完成")
    logger.info(f"[{PLUGIN_NAME}] 批量补图启动：{len(cands)} 角色 / {n} 张"
                f"（require_ref={require_ref}, 跳过 {len(skipped)}）")
    return ok({"queued": n, "chars": len(cands), "skipped": len(skipped)})


async def h_batch_status(plugin) -> dict:
    """队列进度快照（工坊 2s 轮询）。"""
    return ok({"batch": _batch_runner(plugin).snapshot(),
               "quota": max(1, int(plugin._cfg("portraits_per_char", 5)))})


async def h_batch_stop(plugin) -> dict:
    """请求停止：当前张完成后不再开始下一张（在跑的单张不中断）。"""
    was_running = _batch_runner(plugin).stop()
    return ok({"stopped": True, "was_running": was_running})


async def h_workflows(plugin) -> dict:
    """直接读萌绘配置的工作流条目，按用户口径计算图生图能力（v0.3.6）。

    不再解析 comfyui_workflows 文本——其 [支持图生图] 标记只看手填 image_node，
    新版工作流（base_id 引用基础工作流库）节点以解析为准、配置字段不参与，
    标记必然失真（用户指出的「新版不是根据这个来的」）。
    现逻辑：绑定基础工作流 → 查 anima.workflow_store 的解析注记里有没有图节点；
    未绑定 → 名字带「图生图」；手填 image_node 亦可用。
    萌绘实例经 context.get_registered_star("astrbot_plugin_comfyui_anima").star_cls
    获取（4.29.4 实测接口），其 config/workflow_store 均为插件自身属性。
    """
    meta = None
    try:
        meta = plugin.context.get_registered_star("astrbot_plugin_comfyui_anima")
    except Exception:
        meta = None
    anima = getattr(meta, "star_cls", None) if meta is not None else None
    cfg = getattr(anima, "config", None)
    raw = cfg.get("workflows") if isinstance(cfg, dict) else None
    if not isinstance(raw, list) or not raw:
        return ok({"available": False, "workflows": [],
                   "reason": "萌绘未安装或未配置任何工作流"})
    store = getattr(anima, "workflow_store", None)
    workflows = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        base_roles = None
        base_id = str(entry.get("base_id") or "").strip()
        if base_id and store is not None:
            try:
                rec = store.get(int(base_id))
                base_roles = (rec or {}).get("roles") or {}
            except Exception:
                base_roles = None  # 查询失败按可用兜底（见 draw.workflow_img2img_capable）
        workflows.append({
            "name": name,
            "img2img": draw.workflow_img2img_capable(entry, base_roles),
            "anima": bool(entry.get("is_anima", True)),
        })
    return ok({"available": bool(workflows), "workflows": workflows, "reason": ""})


# ---------- 配置（schema 白名单 + 类型校验，星语同款） ----------

_SKIP = object()


def _schema_of(plugin) -> dict:
    try:
        path = Path(__file__).resolve().parent / "_conf_schema.json"
        import json

        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _coerce(value, meta):
    """按 schema 类型收敛；不合法返回 _SKIP（调用方记 skipped，绝不写脏配置）。"""
    t = str((meta or {}).get("type") or "string").lower()
    try:
        if t == "int":
            return int(value)
        if t == "float":
            return float(value)
        if t == "bool":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "on", "yes")
        if value is None:  # 下拉 clearable 会给 null → 存空串（= 回落默认）
            return ""
        s = str(value)
        opts = (meta or {}).get("options")
        if isinstance(opts, list) and opts and s not in [str(o) for o in opts]:
            return _SKIP
        return s
    except Exception:
        return _SKIP


async def h_config_get(plugin) -> dict:
    schema = _schema_of(plugin)
    items = []
    for key, meta in schema.items():
        if not isinstance(meta, dict):
            continue
        default = meta.get("default")
        items.append({
            "key": key,
            "description": str(meta.get("description") or key),
            "hint": str(meta.get("hint") or ""),
            "type": str(meta.get("type") or "string"),
            "options": meta.get("options"),
            "labels": meta.get("labels"),
            "default": default,
            "value": plugin._cfg(key, default),
        })
    group_pools = str(plugin._cfg("group_pools", "") or "")
    return ok({
        "items": items, "version": _version_of(),
        # 时间显示与「已绑定群」快捷标签用：前端不该再去猜时区或让用户盲填群号
        "timezone": str(plugin._cfg("timezone", "Asia/Shanghai")),
        "bound_groups": sorted(pool.parse_group_pools(group_pools).keys()),
    })


async def h_config_set(plugin) -> dict:
    body = await _body()
    values = body.get("values")
    if not isinstance(values, dict) or not values:
        return err("缺少 values 字段（形状：{values: {配置键: 值}}）")
    schema = _schema_of(plugin)
    if not schema:
        return err("读取插件配置 schema 失败，已拒绝写入（避免写坏配置）")
    cfg = getattr(plugin, "config", None)
    if cfg is None or not callable(getattr(cfg, "save_config", None)):
        return err("插件配置不可写（self.config 缺失）")
    changed: list = []
    skipped: list = []
    for key, value in values.items():
        meta = schema.get(key)
        if meta is None:  # schema 之外的键一律忽略（防越权写入）
            skipped.append(f"{key}（未在 _conf_schema.json 中定义）")
            continue
        coerced = _coerce(value, meta)
        if coerced is _SKIP:
            skipped.append(f"{key}（类型或取值不符合 schema）")
            continue
        try:
            cfg[key] = coerced
            changed.append(key)
        except Exception:
            skipped.append(f"{key}（写入失败）")
    if changed:
        try:
            cfg.save_config()
        except Exception:
            logger.error(f"[{PLUGIN_NAME}] 配置写盘失败\n{traceback.format_exc()}")
            return err("配置写盘失败，请查看控制台日志")
    return ok({"changed": changed, "skipped": skipped})


# ---------- 工坊页签路由记忆（v0.5.12） ----------
# 页面跑在无 allow-same-origin 的 sandbox iframe 里（localStorage 一碰就抛
# SecurityError），Dashboard 刷新时又按服务端 content_path 重建 iframe src，
# 页内 hash 留不住——所以「刷新停在上次页签」只能落在服务端。

# 页签白名单：与前端 menuOpts 的 key 一致，防越权写入。
# v0.8.0：角色详情改为二级页面（不再是导航页签，旧记忆值自动回落 chars）。
# v0.9.0：原「记录 · 调试」拆成 抽签记录 / 数据统计 / 用户查询 / 调试工具 四页，
# 旧键 records 保留（老记忆值仍落在抽签记录页，不会跳回角色库）。
# v0.10.0：新增「总览」并放到第一栏，同时成为无记忆时的默认页签。
_UI_MENUS = ("overview", "chars", "works", "try", "records", "stats",
             "users", "debug", "bind", "config")


def _ui_state_path(plugin) -> Path:
    return Path(plugin._data_dir) / "ui_state.json"


async def h_ui_state_get(plugin) -> dict:
    menu = "overview"
    try:
        data = _json_load(_ui_state_path(plugin))
        if isinstance(data, dict) and data.get("menu") in _UI_MENUS:
            menu = data["menu"]
    except FileNotFoundError:
        pass
    except Exception:
        logger.warning(f"[{PLUGIN_NAME}] ui_state 读取失败，回落默认页签\n{traceback.format_exc()}")
    return ok({"menu": menu})


async def h_ui_state_set(plugin) -> dict:
    body = await _body()
    menu = body.get("menu")
    if menu not in _UI_MENUS:
        return err(f"menu 必须是 {' / '.join(_UI_MENUS)} 之一")
    try:
        _json_save(_ui_state_path(plugin), {"menu": menu})
    except Exception:
        logger.warning(f"[{PLUGIN_NAME}] ui_state 写盘失败\n{traceback.format_exc()}")
        return err("页签记忆写盘失败")
    return ok({"menu": menu})


async def h_overview(plugin) -> dict:
    """总览页聚合（只读）：库存规模 / 出卡源配置 / 抽卡概况 / 最近记录。"""
    def build():
        packs = pool.load_packs(_pools_dir(plugin), logger)
        entries = []
        for p in packs:
            for raw in p["entries"]:
                e = pool.normalize_entry(raw, p["pack"])
                if e:
                    entries.append(e)
        quota = max(1, int(plugin._cfg("portraits_per_char", 5) or 5))
        portraits = plugin._store.list_portraits_all() if plugin._store else []
        by_char = {}
        for row in portraits:
            by_char[row["char_id"]] = by_char.get(row["char_id"], 0) + 1

        refs_bound = 0
        draw_sources = 0
        no_source = []
        for e in entries:
            st = e.get("strategy") or {}
            refs = st.get("refs") if isinstance(st.get("refs"), list) else []
            refs_bound += sum(1 for x in refs if str(x or "").strip())
            has_draw = bool(draw.draw_source(e, plugin._data_dir))
            if has_draw:
                draw_sources += 1
            if str(e.get("strategy_type") or "") == "img2img" \
                    and not has_draw and not draw.explicit_ref(st, plugin._data_dir):
                no_source.append({"id": e["id"], "name": e["name"]})

        today = plugin._today()
        recent, today_draws, total_draws = [], 0, 0
        if plugin._store:
            recent = plugin._store.list_records(1, 5)["rows"]
            today_draws = plugin._store.list_records(1, 1, today, today)["total"]
            total_draws = plugin._store.list_records(1, 1)["total"]
        try:
            anima_ready = draw._get_tool_handler(plugin.context, "comfyui_draw") is not None
        except Exception:
            anima_ready = False
        return {
            "version": _version_of(), "anima_ready": anima_ready,
            "quota": quota,
            "gen_source_mode": str(plugin._cfg("gen_source_mode", "mix") or "mix"),
            "img2img_prob": _int_clamp(plugin._cfg("img2img_prob", 50), 50, 0, 100),
            "packs": len(packs), "chars": len(entries),
            "verified": sum(1 for e in entries if e["verified"]),
            "portraits": len(portraits),
            "chars_full": sum(1 for n in by_char.values() if n >= quota),
            "refs_bound": refs_bound, "orphans": len(_orphan_items(plugin)),
            "draw_sources": draw_sources, "no_source": no_source[:6],
            "today_draws": today_draws, "total_draws": total_draws,
            "recent": recent,
        }
    return ok(await asyncio.to_thread(build))


# ---------- 记录 / 统计 / 调试（v0.8.0） ----------

def _name_color_map(plugin) -> dict:
    """角色 id → (名称, 颜色, 卷名)，统计榜显示用。"""
    name_of: dict = {}
    for pack in pool.load_packs(_pools_dir(plugin), logger):
        for raw in pack["entries"]:
            e = pool.normalize_entry(raw, pack["pack"])
            if e:
                name_of.setdefault(e["id"], (e["name"], e["color"], pack["pack"]))
    return name_of


def _delete_card_files(plugin, paths) -> int:
    """删 data 目录内的卡图/立绘文件（越界或不存在则跳过）。返回实际删除数。"""
    n = 0
    for p in paths or []:
        fp = _safe_data_file(plugin, str(p or ""))
        if fp is None:
            continue
        try:
            fp.unlink()
            n += 1
        except Exception:
            logger.warning(f"[{PLUGIN_NAME}] 删除文件失败：{fp}")
    return n


def _trash_files(plugin, paths) -> tuple:
    """把 data 目录内的文件**移进回收站**而非直接删除，返回 (移动数, 回收站目录)。

    v0.19.5 事故教训（用户实测）：调试页「清空老婆立绘库」直接把 portraits/
    下的文件 unlink 了，误点之后角色的立绘彻底找不回来。调试类破坏性动作一律
    先搬到 `data/_trash/<时间戳>/<原相对路径>`——误点还能手动搬回原处，
    而且不会悄悄占着 portraits/（不影响库存计数与配额判定）。
    """
    moved = 0
    if not paths:
        return moved, ""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    trash_root = Path(plugin._data_dir) / "_trash" / stamp
    try:
        data_root = Path(plugin._data_dir).resolve()
    except Exception:
        return moved, ""
    for p in paths:
        fp = _safe_data_file(plugin, str(p or ""))
        if fp is None:
            continue
        try:
            rel = fp.resolve().relative_to(data_root)
        except Exception:
            rel = Path(fp.name)
        dest = trash_root / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(fp), str(dest))
            moved += 1
        except Exception:
            logger.warning(f"[{PLUGIN_NAME}] 移入回收站失败：{fp}")
    return moved, (str(trash_root) if moved else "")


async def h_records_draws(plugin) -> dict:
    """抽取记录分页（draws 表，updated_at 倒序）。可按 user_id / date 过滤。"""
    if not plugin._store:
        return err("插件未初始化完成")
    body = await _body()
    uid = str(body.get("user_id") or _q("user_id", "") or "").strip()
    date = str(body.get("date") or _q("date", "") or "").strip()
    try:
        limit = max(1, min(500, int(body.get("limit") or _q("limit", 100) or 100)))
        offset = max(0, int(body.get("offset") or _q("offset", 0) or 0))
    except (TypeError, ValueError):
        limit, offset = 100, 0
    rows, total = plugin._store.list_draws(limit, offset, uid, date)
    return ok({"items": rows, "total": total, "limit": limit, "offset": offset})


async def h_records_jobs(plugin) -> dict:
    """生图审计分页（draw_jobs 表）。可按 char_id / status / mode 过滤。"""
    if not plugin._store:
        return err("插件未初始化完成")
    body = await _body()
    cid = str(body.get("char_id") or _q("char_id", "") or "").strip()
    status = str(body.get("status") or _q("status", "") or "").strip()
    mode = str(body.get("mode") or _q("mode", "") or "").strip()
    try:
        limit = max(1, min(500, int(body.get("limit") or _q("limit", 100) or 100)))
        offset = max(0, int(body.get("offset") or _q("offset", 0) or 0))
    except (TypeError, ValueError):
        limit, offset = 100, 0
    rows, total = plugin._store.list_jobs(limit, offset, cid, status, mode)
    return ok({"items": rows, "total": total, "limit": limit, "offset": offset})


async def h_records_stats(plugin) -> dict:
    """总览统计：抽取侧 + 生成侧汇总 + 用户榜 / 角色热度榜。"""
    if not plugin._store:
        return err("插件未初始化完成")
    today = plugin._today()
    name_of = _name_color_map(plugin)

    def with_name(rows):
        out = []
        for r in rows:
            r = dict(r)
            meta = name_of.get(str(r.get("char_id") or ""))
            r["char_name"] = meta[0] if meta else (str(r.get("char_id") or ""))
            r["color"] = meta[1] if meta else "#8A8A9A"
            out.append(r)
        return out

    return ok({
        "today": today,
        "draw": plugin._store.draw_stats(today),
        "job": plugin._store.job_stats(today),
        "top_users": plugin._store.top_users(10),
        "top_chars": with_name(plugin._store.top_chars(10)),
    })


async def h_debug_clear_draws(plugin) -> dict:
    """清抽取签：可按 user_id / date 过滤（都空 = 全部），连带删除卡图文件。"""
    if not plugin._store:
        return err("插件未初始化完成")
    body = await _body()
    uid = str(body.get("user_id") or "").strip()
    date = str(body.get("date") or "").strip()
    if not uid and not date:
        return err("清空全部签属于高危操作：请明确指定 user_id 或 date")
    rows = plugin._store.clear_draws(uid, date)
    # v0.19.5：卡图也进回收站（误清还能手动找回），不再 unlink
    trashed, trash_dir = _trash_files(plugin, [r.get("card_path") for r in rows])
    users_reset = len({r.get("user_id") for r in rows if r.get("user_id")})
    plugin._store.debug_log("reset", uid or date,
                            f"清抽取签 {len(rows)} 条（{date or '全部日期'}），"
                            f"扣减计数并重派 {users_reset} 位用户种子")
    return ok({"cleared": len(rows), "cards_deleted": trashed,
               "cards_trashed": trashed, "trash_dir": trash_dir,
               "users_reset": users_reset})


async def h_debug_force_reroll(plugin) -> dict:
    """强制换签（调试版换老婆）：nonce+1 按新种子重派角色、清卡图。

    直接改库，不受 reroll_daily_limit 限制。v0.9.4 起除了 nonce+1 还会
    真正重算 char_id（旧实现只加 nonce 不换人——/今日老婆 对已有签只重渲染
    旧角色卡，导致"强制换签抽出来还是同一个人"）；候选为空时保留旧角色
    只加 nonce。角色候选集沿用该签当时的 group_id（群绑定口径一致）。
    """
    if not plugin._store:
        return err("插件未初始化完成")
    body = await _body()
    uid = str(body.get("user_id") or "").strip()
    if not uid:
        return err("缺少 user_id")
    date = str(body.get("date") or plugin._today()).strip()
    old = plugin._store.get_draw(uid, date)
    row = plugin._store.force_reroll(uid, date)
    if row is None:
        return err(f"{uid} 在 {date} 没有抽取记录")
    nonce = int(row.get("nonce") or 0)
    seed = pool.derive_seed(date, uid, str(plugin._cfg("salt", "moe-waifu")), nonce,
                            plugin._store.get_epoch(uid))
    candidates = pool.candidates_for(
        plugin._load_packs(), str(row.get("group_id") or ""),
        str(plugin._cfg("group_pools", "") or ""),
        bool(plugin._cfg("allow_unverified", False)),
    )
    reassigned = ""
    if candidates:
        entry = pool.pick_character(seed, candidates)
        if entry["id"] != row.get("char_id"):
            plugin._store.update_draw_char(
                uid, date, entry["id"],
                char_name=str(entry.get("name") or ""),
                series=str(entry.get("series") or ""),
                seed_hex=seed.hex(),
            )
            reassigned = pool.entry_label(entry)
    row = plugin._store.get_draw(uid, date) or row
    deleted = _delete_card_files(plugin, [(old or {}).get("card_path")])
    detail = f"{date} 强制换签 → nonce={nonce}"
    if reassigned:
        detail += f"，重派 {reassigned}"
    plugin._store.debug_log("reset", uid, detail)
    return ok({"row": row, "reassigned": reassigned, "cards_deleted": deleted})


async def h_debug_reset_char_day(plugin) -> dict:
    """重试某角色当日生成：删当日立绘行与文件、清指向它的签卡路径，
    下次有人抽到该角色即重新生成。"""
    if not plugin._store:
        return err("插件未初始化完成")
    body = await _body()
    cid = str(body.get("char_id") or "").strip()
    if not cid:
        return err("缺少 char_id")
    date = str(body.get("date") or plugin._today()).strip()
    res = plugin._store.reset_char_day(cid, date)
    trashed, trash_dir = _trash_files(plugin, res["removed_files"])
    plugin._store.debug_log("reset", cid,
                            f"{date} 重试当日生成，移入回收站 {trashed} 张")
    return ok({"char_id": cid, "date": date, "cleared_draws": res["cleared_draws"],
               "files_deleted": trashed, "files_trashed": trashed,
               "trash_dir": trash_dir})


async def h_debug_clear_portraits(plugin) -> dict:
    """清空某角色全部立绘库存（行 + 文件移入回收站），下次抽取从 0 重新生成。

    ⚠️ 作用对象是**角色**，不是某个 QQ 用户的抽签记录：
    清掉的是「角色详情」里那面立绘墙（所有用户共用），与 draws/draw_events 无关。
    v0.19.5 事故：用户点调试页的「清空老婆立绘库」以为是清某个用户的抽取记录，
    结果角色立绘全没了（当时是直接 unlink，无法恢复）。两道修正：
      ① 必须显式带 confirm=true 才执行（防误点/误调）；
      ② 文件 `shutil.move` 进 data/_trash/<时间戳>/，误点还能手动搬回。
    """
    if not plugin._store:
        return err("插件未初始化完成")
    body = await _body()
    cid = str(body.get("char_id") or "").strip()
    if not cid:
        return err("缺少 char_id")
    if not bool(body.get("confirm")):
        return err("该操作清空的是「角色」的立绘库（不是某个用户的抽签记录），"
                   "请带 confirm=true 再次提交")
    files = plugin._store.clear_char_portraits(cid)
    trashed, trash_dir = _trash_files(plugin, files)
    plugin._store.debug_log(
        "reset", cid,
        f"清空立绘库 {len(files)} 张（回收站：{trash_dir or '—'}）")
    logger.warning(f"[{PLUGIN_NAME}] 清空角色立绘库 {cid}：{len(files)} 张，"
                   f"已移入 {trash_dir or '（无文件）'}")
    return ok({"char_id": cid, "cleared": len(files), "files_deleted": trashed,
               "files_trashed": trashed, "trash_dir": trash_dir})


# ---------- 抽签记录 / 统计 / 用户查询 / 调试（v0.9.0） ----------
# 参照萌萌星语的分层：handler 只夹参数 + asyncio.to_thread，业务全在同步
# _xxx_core 里——不启 quart 也能直接喂 SimpleNamespace 桩跑真 SQL。

def _int_clamp(value, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def _range_of(plugin, days=30, date_from="", date_to="") -> tuple[str, str]:
    """按插件时区算「含今天」的区间；显式传日期时以传参为准。"""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(str(plugin._cfg("timezone", "Asia/Shanghai")))
        end = datetime.now(tz).date()
    except Exception:
        end = datetime.now().date()
    start = end - timedelta(days=max(1, min(180, int(days))) - 1)
    return (str(date_from or start.isoformat()), str(date_to or end.isoformat()))


def _enrich_char_rows(rows, name_of: dict, id_key: str = "char_id") -> None:
    """角色名快照为空（老库 / 角色已删）时用当前角色库回填，颜色也一并补上。

    只改返回值、不写库：历史记录读得懂，但不会把已删角色「复活」进库。
    """
    for r in rows:
        meta = name_of.get(str(r.get(id_key) or ""))
        if not r.get("char_name"):
            r["char_name"] = meta[0] if meta else str(r.get(id_key) or "")
        r["char_color"] = meta[1] if meta else "#8A8A9A"
        r["pack"] = meta[2] if meta else ""


def _mask_salt(salt: str) -> str:
    s = str(salt or "")
    return s if len(s) <= 2 else f"{s[:2]}***{s[-1:]}"


# ---- core ----

def _records_core(plugin, p: dict) -> dict:
    data = plugin._store.list_records(
        page=_int_clamp(p.get("page"), 1, 1, 100000),
        size=_int_clamp(p.get("size"), 20, 1, 100),
        date_from=str(p.get("date_from") or ""), date_to=str(p.get("date_to") or ""),
        q=str(p.get("q") or ""), char_id=str(p.get("char_id") or ""),
        group_id=str(p.get("group_id") or ""), platform=str(p.get("platform") or ""),
        rerolled=str(p.get("rerolled") or ""),
    )
    _enrich_char_rows(data["rows"], _name_color_map(plugin))
    return data


def _record_detail_core(plugin, uid: str, date: str) -> dict | None:
    detail = plugin._store.record_detail(uid, date)
    if detail is None:
        return None
    name_of = _name_color_map(plugin)
    _enrich_char_rows([detail["draw"]], name_of)
    _enrich_char_rows(detail["events"], name_of)
    _enrich_char_rows(detail["jobs"], name_of)
    return detail


def _stats_core(plugin, days=30, date_from="", date_to="") -> dict:
    lo, hi = _range_of(plugin, days, date_from, date_to)
    data = plugin._store.stats_range(
        lo, hi, tz_name=str(plugin._cfg("timezone", "Asia/Shanghai")))
    _enrich_char_rows(data["chars"], _name_color_map(plugin))
    data["today"] = plugin._today()
    return data


def _users_core(plugin, page, size, q, order) -> dict:
    data = plugin._store.list_users(
        _int_clamp(page, 1, 1, 100000), _int_clamp(size, 24, 1, 100),
        str(q or ""), str(order or "last"), plugin._today())
    return data


def _user_core(plugin, uid: str, months=3) -> dict:
    prof = plugin._store.user_profile(uid, plugin._today(),
                                      _int_clamp(months, 3, 1, 12))
    if prof is None:
        return {"found": False, "user_id": uid}
    prof["found"] = True
    name_of = _name_color_map(plugin)
    _enrich_char_rows(prof["history"], name_of)
    _enrich_char_rows(prof["events"], name_of)
    _enrich_char_rows(prof["jobs"], name_of)
    _enrich_char_rows(prof["calendar"], name_of)
    _enrich_char_rows(prof["top_chars"], name_of)
    if prof.get("today"):
        _enrich_char_rows([prof["today"]], name_of)
    return prof


def _debug_state_core(plugin, uid: str) -> dict:
    s = plugin._store
    today = plugin._today()
    row = s.get_draw(uid, today) if uid else None
    card_exists = False
    if row and row.get("card_path"):
        try:
            card_exists = Path(str(row["card_path"])).is_file()
        except Exception:
            pass
    events, events_total = s.list_events(10, 0, uid) if uid else ([], 0)
    jobs, jobs_total = s.list_jobs(10, 0, user_id=uid) if uid else ([], 0)
    name_of = _name_color_map(plugin)
    if row:
        _enrich_char_rows([row], name_of)
    _enrich_char_rows(events, name_of)
    _enrich_char_rows(jobs, name_of)
    packs = pool.load_packs(_pools_dir(plugin), logger)
    group_pools = str(plugin._cfg("group_pools", "") or "")
    allow_unverified = bool(plugin._cfg("allow_unverified", False))
    candidates = pool.candidates_for(packs, "", group_pools, allow_unverified)
    portraits = s.count_portraits(row["char_id"]) if row else 0
    return {
        "user_id": uid, "today": today,
        "draw": row, "card_exists": card_exists,
        "portraits_of_current": portraits,
        "nonce": int(row["nonce"]) if row else 0,
        "reroll_limit": int(plugin._cfg("reroll_daily_limit", 1)),
        "events": events, "events_total": events_total,
        "jobs": jobs, "jobs_total": jobs_total,
        "pool": {"packs": len(packs), "candidates": len(candidates),
                 "verified": sum(1 for p in packs for e in p["entries"] if e.get("verified"))},
        "config": {
            "timezone": str(plugin._cfg("timezone", "Asia/Shanghai")),
            "salt": _mask_salt(str(plugin._cfg("salt", "moe-waifu"))),
            "reroll_daily_limit": int(plugin._cfg("reroll_daily_limit", 1)),
            "portraits_per_char": int(plugin._cfg("portraits_per_char", 5)),
            "allow_unverified": allow_unverified,
            "group_pools": group_pools,
            "draw_workflow": str(plugin._cfg("draw_workflow", "") or ""),
            "draw_timeout": int(plugin._cfg("draw_timeout", 120)),
        },
    }


def _debug_simulate_core(plugin, uid: str, date: str, nonce: int,
                         salt: str, group_id: str) -> dict:
    """只读模拟抽签（dry-run）：不写库、不生图、不发消息。

    复用真实链路的纯函数 derive_seed / candidates_for / decision_of / pool_index，
    所以预览结果和 `/今日老婆` 完全一致；改 nonce 就能看清「换老婆」会换到谁。
    """
    packs = pool.load_packs(_pools_dir(plugin), logger)
    group_pools = str(plugin._cfg("group_pools", "") or "")
    allow_unverified = bool(plugin._cfg("allow_unverified", False))
    candidates = pool.candidates_for(packs, group_id, group_pools, allow_unverified)
    # 把「该群绑定作品 ∩ verified」这条规则的实际生效范围一并返回，
    # 前端才说得清为什么候选是 N 位（群号留空时 = 全部启用卷）。
    bound = pool.parse_group_pools(group_pools)
    want = pool.series_for_group(bound, group_id)  # 作品集合或 None
    scope = {
        "group_series": list(want) if want is not None else None,  # None = 未绑定（含私聊）→ 全部启用卷
        "enabled_packs": [p["pack"] for p in packs
                          if p["enabled"] and (want is None or any(
                              (e.get("series") or "") in want for e in p.get("entries", [])))],
        "allow_unverified": allow_unverified,
    }
    if not candidates:
        return {"ok": False, "readonly": True,
                "reason": "候选角色为空：请检查启用卷、群绑定与 verified 标记",
                "candidates": 0, "scope": scope,
                "note": "只读预览不会写库；把角色卷启用或标为 verified 后再试"}
    seed = pool.derive_seed(date, uid, salt, nonce,
                            plugin._store.get_epoch(uid) if plugin._store else 0)
    index = pool.sub_int(seed, "char", len(candidates))
    entry = candidates[index]
    s = plugin._store
    today_row = s.portrait_by_date(entry["id"], date)
    pool_size = s.count_portraits(entry["id"])
    quota = max(1, int(plugin._cfg("portraits_per_char", 5)))
    gen_mode = str(plugin._cfg("gen_source_mode", "mix") or "mix")
    # v0.19.3：mode 参与决策（img2img 不受满配额短路），模拟与真实出卡同口径
    decision = draw.decision_of(entry["id"], date, today_row, pool_size, quota, gen_mode)
    try:
        i2i_prob = int(plugin._cfg("img2img_prob", 50))
    except (TypeError, ValueError):
        i2i_prob = 50
    has_src = draw.has_draw_source(entry, plugin._data_dir)
    roll = pool.sub_int(seed, "genmode", 100)
    if decision == "generate":
        # 与真实出卡同口径：只认显式指定的出卡图生图源（v0.10.0）
        decision = draw.plan_source(gen_mode, pool_size, quota, has_src, roll, i2i_prob)
    # v0.19.4：模拟也给出「为什么这么出图」，与落库的 decision_note 同一句
    decision_note = draw.explain_decision(gen_mode, decision, pool_size, quota,
                                          has_src, i2i_prob, roll,
                                          today_portrait=bool(today_row))
    pick = None
    if decision == "reuse_today":
        pick = {"file": today_row["file"] if today_row else "", "index": None}
    elif decision == "from_pool":
        rows = s.list_portraits(entry["id"])
        pidx = pool.pool_index(entry["id"], date, pool_size, salt) % len(rows) if rows else 0
        pick = {"file": rows[pidx]["file"] if rows else "", "index": pidx}
    current = s.get_draw(uid, date)
    return {
        "ok": True, "readonly": True,
        "user_id": uid, "date": date, "nonce": int(nonce),
        "group_id": group_id, "seed_hex": seed.hex(),
        "candidates": len(candidates), "index": index, "scope": scope,
        "char": {
            "char_id": entry["id"], "char_name": entry["name"],
            "series": entry.get("series") or "", "pack": entry.get("pack") or "",
            "category": entry.get("category") or "", "tier": entry.get("tier") or "",
            "color": entry.get("color") or "#E8B4C8",
            "strategy_type": entry.get("strategy_type") or "base",
            "verified": bool(entry.get("verified")),
        },
        "decision": decision, "decision_note": decision_note,
        "portrait_pool_size": pool_size, "quota": quota,
        "gen_mode": gen_mode, "i2i_prob": i2i_prob,
        "portrait_pick": pick,
        "current": ({"char_id": current["char_id"],
                     "char_name": current.get("char_name") or current["char_id"],
                     "nonce": int(current["nonce"])} if current else None),
        "matches_current": bool(current and current["char_id"] == entry["id"]),
        "note": "只读预览：不写库、不生图、不发消息，也不消耗换老婆次数",
    }


# ---- handlers ----

_RECORD_KEYS = ("page", "size", "date_from", "date_to", "q", "char_id",
                "group_id", "platform", "rerolled")


def _params(body: dict, keys) -> dict:
    return {k: (body.get(k) if body.get(k) not in (None, "") else _q(k, "")) for k in keys}


async def h_records(plugin) -> dict:
    if not plugin._store:
        return err("插件未初始化完成")
    body = await _body()
    return ok(await asyncio.to_thread(_records_core, plugin, _params(body, _RECORD_KEYS)))


async def h_record_detail(plugin) -> dict:
    if not plugin._store:
        return err("插件未初始化完成")
    body = await _body()
    uid = str(body.get("user_id") or _q("user_id", "") or "").strip()
    date = str(body.get("date") or _q("date", "") or "").strip()
    if not uid or not date:
        return err("缺少 user_id 或 date")
    data = await asyncio.to_thread(_record_detail_core, plugin, uid, date)
    if data is None:
        return err(f"{uid} 在 {date} 没有抽取记录")
    return ok(data)


async def h_record_events(plugin) -> dict:
    if not plugin._store:
        return err("插件未初始化完成")
    body = await _body()
    uid = str(body.get("user_id") or _q("user_id", "") or "").strip()
    date = str(body.get("date") or _q("date", "") or "").strip()
    kind = str(body.get("kind") or _q("kind", "") or "").strip()
    limit = _int_clamp(body.get("limit") or _q("limit", 50), 50, 1, 200)
    offset = _int_clamp(body.get("offset") or _q("offset", 0), 0, 0, 100000)
    rows, total = await asyncio.to_thread(
        plugin._store.list_events, limit, offset, uid, date, kind)
    _enrich_char_rows(rows, _name_color_map(plugin))
    return ok({"total": total, "rows": rows})


async def h_stats(plugin) -> dict:
    if not plugin._store:
        return err("插件未初始化完成")
    body = await _body()
    days = _int_clamp(body.get("days") or _q("days", 30), 30, 1, 180)
    lo = str(body.get("date_from") or _q("date_from", "") or "")
    hi = str(body.get("date_to") or _q("date_to", "") or "")
    return ok(await asyncio.to_thread(_stats_core, plugin, days, lo, hi))


async def h_users(plugin) -> dict:
    if not plugin._store:
        return err("插件未初始化完成")
    body = await _body()
    return ok(await asyncio.to_thread(
        _users_core, plugin,
        body.get("page") or _q("page", 1), body.get("size") or _q("size", 24),
        body.get("q") or _q("q", ""), body.get("order") or _q("order", "last")))


async def h_user(plugin) -> dict:
    if not plugin._store:
        return err("插件未初始化完成")
    body = await _body()
    uid = str(body.get("uid") or body.get("user_id") or _q("uid", "") or "").strip()
    if not uid:
        return err("缺少 uid")
    await asyncio.to_thread(plugin._store.debug_log, "query", uid, "用户查询")
    return ok(await asyncio.to_thread(
        _user_core, plugin, uid, body.get("months") or _q("months", 3)))


# ---------- 用户头像（v0.13.2） ----------
# 前端在记录表 / 用户卡 / 总览批量展示头像，逐个直连 QQ 图床既慢又不稳，
# 统一走本代理：q4.qlogo.cn/headimg_dl（q1 g?qq= 端点已死，星语同款通道）
# 拉 spec=100 小图转 data URL；进程内缓存 + TTL，失败返回 err（前端静默回落色块）。

_AVATAR_TTL = 6 * 3600
_AVATAR_CACHE_MAX = 500
_avatar_cache: dict[str, tuple[float, str]] = {}


async def _fetch_avatar_data_url(uid: str) -> str | None:
    try:
        import aiohttp  # AstrBot 运行期依赖；缺席时返回 None 走降级
    except Exception:
        return None

    url = f"https://q4.qlogo.cn/headimg_dl?qq={uid}&spec=100"
    try:
        timeout = aiohttp.ClientTimeout(total=6)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                raw = await resp.read()
    except Exception:
        return None
    if not raw:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")


async def h_user_avatar(plugin) -> dict:
    body = await _body()
    uid = str(body.get("uid") or _q("uid", "") or "").strip()
    if not uid or not uid.isdigit():
        return err("缺少合法 uid")
    now = time.time()
    hit = _avatar_cache.get(uid)
    if hit and now - hit[0] < _AVATAR_TTL:
        return ok({"data_url": hit[1], "cached": True})
    data_url = await _fetch_avatar_data_url(uid)
    if not data_url:
        return err("头像获取失败")
    if len(_avatar_cache) >= _AVATAR_CACHE_MAX:
        # 简单淘汰：丢掉最旧的一半，避免无限增长
        for k in sorted(_avatar_cache, key=lambda k: _avatar_cache[k][0])[
            : _AVATAR_CACHE_MAX // 2
        ]:
            _avatar_cache.pop(k, None)
    _avatar_cache[uid] = (now, data_url)
    return ok({"data_url": data_url, "cached": False})


async def h_filter_options(plugin) -> dict:
    """记录筛选下拉：群 / 平台 / 角色（一次取齐，避免每开一页发三个请求）。"""
    if not plugin._store:
        return err("插件未初始化完成")

    def build():
        name_of = _name_color_map(plugin)
        chars = [{"char_id": cid, "char_name": meta[0], "char_color": meta[1],
                  "pack": meta[2]} for cid, meta in sorted(name_of.items())]
        return {"groups": plugin._store.list_groups(),
                "platforms": plugin._store.list_platforms(), "chars": chars}
    return ok(await asyncio.to_thread(build))


async def h_debug_state(plugin) -> dict:
    if not plugin._store:
        return err("插件未初始化完成")
    body = await _body()
    uid = str(body.get("uid") or body.get("user_id") or _q("uid", "") or "").strip()
    if not uid:
        return err("缺少 uid")
    await asyncio.to_thread(plugin._store.debug_log, "query", uid, "调试状态查询")
    return ok(await asyncio.to_thread(_debug_state_core, plugin, uid))


async def h_debug_history(plugin) -> dict:
    if not plugin._store:
        return err("插件未初始化完成")
    body = await _body()
    limit = _int_clamp(body.get("limit") or _q("limit", 30), 30, 1, 200)
    recent = await asyncio.to_thread(plugin._store.debug_recent, 8)
    audit = await asyncio.to_thread(plugin._store.debug_audit, limit)
    return ok({"recent": recent, "audit": audit})


async def h_debug_simulate(plugin) -> dict:
    if not plugin._store:
        return err("插件未初始化完成")
    body = await _body()
    uid = str(body.get("user_id") or _q("user_id", "") or "").strip()
    if not uid:
        return err("缺少 user_id")
    date = str(body.get("date") or _q("date", "") or "").strip() or plugin._today()
    nonce = _int_clamp(body.get("nonce") or _q("nonce", 0), 0, 0, 9999)
    salt = str(body.get("salt") or _q("salt", "") or "").strip() \
        or str(plugin._cfg("salt", "moe-waifu"))
    gid = str(body.get("group_id") or _q("group_id", "") or "").strip()
    data = await asyncio.to_thread(_debug_simulate_core, plugin, uid, date, nonce, salt, gid)
    await asyncio.to_thread(plugin._store.debug_log, "simulate", uid,
                            f"{date} nonce={nonce} → {data.get('char', {}).get('char_name', '-')}")
    return ok(data)


async def h_debug_rerender(plugin) -> dict:
    """按已存签重渲染卡片（改了 card.py 后看效果），不重抽、不生图。"""
    if not plugin._store:
        return err("插件未初始化完成")
    body = await _body()
    uid = str(body.get("user_id") or _q("user_id", "") or "").strip()
    if not uid:
        return err("缺少 user_id")
    date = str(body.get("date") or _q("date", "") or "").strip() or plugin._today()
    row = plugin._store.get_draw(uid, date)
    if not row:
        return err(f"{uid} 在 {date} 没有抽取记录")
    packs = pool.load_packs(_pools_dir(plugin), logger)
    entry = pool.find_entry(packs, row["char_id"])
    if entry is None:
        return err("角色条目已不在角色库，无法重渲染")
    rel = str(row.get("portrait_file") or "")
    if not rel:
        pr = plugin._store.portrait_by_date(row["char_id"], date)
        pr = pr or (plugin._store.list_portraits(row["char_id"]) or [None])[-1]
        rel = str(pr["file"]) if pr else ""
    src = Path(plugin._data_dir) / rel
    if not rel or not src.is_file():
        return err("该签的立绘文件已不存在，无法重渲染")
    # 印象语按签随机选句（与 /今日老婆 同口径）：优先用落库的种子快照
    seed_hex = str(row.get("seed_hex") or "")
    if len(seed_hex) == 64:
        seed_bytes = bytes.fromhex(seed_hex)
    else:
        salt = str(plugin._cfg("salt", "moe-waifu"))
        seed_bytes = pool.derive_seed(date, uid, salt, int(row.get("nonce") or 0),
                                      plugin._store.get_epoch(uid))
    entry = dict(entry)
    entry["quote"] = pool.pick_quote(entry, seed_bytes)
    out = Path(plugin._data_dir) / "cards" / f"{uid}_{date}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        from . import card  # 延迟导入：Pillow 只在真的重渲染时才需要
    except ImportError:  # pragma: no cover - 平铺调试
        import card  # type: ignore
    card_path = await asyncio.to_thread(
        card.render_card, src, entry, date_str=date, same_count=1, total_count=1,
        rerolled=bool(row["rerolled"]), out_path=str(out),
        data_dir=Path(plugin._data_dir))
    plugin._store.update_draw_card(uid, date, card_path)
    await asyncio.to_thread(plugin._store.debug_log, "rerender", uid, f"{date} 重渲染卡片")
    return ok({"card_path": card_path, "date": date})


# ---------- 印象语 LLM 补写（v0.9.5） ----------

# 角色卡片「印象语」生成：一句中文台词/印象，显示在立绘卡片上。
_QUOTE_SYSTEM_PROMPT = (
    "你为二次元角色卡片撰写「印象语」——贴在该角色抽卡立绘卡片上的一句话中文"
    "台词或印象描述。要求：贴合角色性格与原作设定、口语自然、每句不超过 30 字；"
    "可以是角色第一人称台词，也可以是简短的旁观印象。输出纪律：每行一句，"
    "不要编号、不要引号、不要 markdown、不要解释。"
)


def _parse_quote_lines(text: str, count: int) -> list:
    """LLM 返回 → 干净的印象语列表（去编号/引号/空行，去重，截到 count）。"""
    out = []
    for line in str(text or "").splitlines():
        line = prompt_gen.sanitize_llm_output(line, limit=60)
        if not line:
            continue
        if line in out:
            continue
        out.append(line)
        if len(out) >= count:
            break
    return out


async def h_quote_gen(plugin) -> dict:
    """LLM 补写角色印象语：按角色名/作品/tags 生成 N 句，前端填进表单再人工确认保存。"""
    body = await _body()
    name = str(body.get("name") or "").strip()
    if not name:
        return err("缺少角色名")
    series = str(body.get("series") or "").strip()
    tags = str(body.get("tags_en") or "").strip()
    try:
        count = max(1, min(10, int(body.get("count") or 5)))
    except (TypeError, ValueError):
        count = 5
    provider = await prompt_gen._resolve_provider(
        plugin.context, str(plugin._cfg("prompt_llm_provider", "") or ""))
    if provider is None:
        return err("未找到可用的对话模型：请先在 AstrBot 配置 LLM，"
                   "或在本插件配置里指定「提示词模型 ID」")
    timeout = int(plugin._cfg("prompt_llm_timeout", 30) or 30) or 30
    user_prompt = (
        f"角色：{name}" + (f"（出自 {series}）" if series else "") + "。\n"
        + (f"外观特征 tags：{tags}。\n" if tags else "")
        + f"请写 {count} 句风格多样的印象语（招呼/傲娇/温柔/俏皮等各来一点，"
          "不要句式雷同）。"
    )
    try:
        resp = await asyncio.wait_for(
            provider.text_chat(prompt=user_prompt, session_id=None,
                               system_prompt=_QUOTE_SYSTEM_PROMPT),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return err(f"LLM 超时（>{timeout}s），稍后再试")
    except Exception as exc:
        return err(f"LLM 调用失败：{exc}")
    quotes = _parse_quote_lines(getattr(resp, "completion_text", "") or "", count)
    if not quotes:
        return err("LLM 没有返回可用句子，请重试")
    return ok({"quotes": quotes})


# ---------- 注册 ----------

def register(plugin) -> None:
    routes = [
        ("/pools", h_pools, ["GET"]),
        ("/works", h_works, ["GET"]),
        ("/workflows", h_workflows, ["GET"]),
        ("/llm_providers", h_llm_providers, ["GET"]),
        ("/themes", h_themes, ["GET"]),
        ("/prompt/preview", h_prompt_preview, ["POST"]),
        ("/prompt/pools", h_prompt_pools, ["GET"]),
        ("/works/discover", h_works_discover, ["POST"]),
        ("/works/discover_add", h_works_discover_add, ["POST"]),
        ("/batch/preview", h_batch_preview, ["POST"]),
        ("/batch/start", h_batch_start, ["POST"]),
        ("/batch/status", h_batch_status, ["GET", "POST"]),
        ("/batch/stop", h_batch_stop, ["POST"]),
        ("/pools/save_entry", h_save_entry, ["POST"]),
        ("/pools/import", h_import_pack, ["POST"]),
        ("/pools/duplicates", h_duplicates, ["GET", "POST"]),
        ("/pools/dedupe", h_dedupe, ["POST"]),
        ("/relational/status", h_relational_status, ["GET", "POST"]),
        ("/pools/delete_entry", h_delete_entry, ["POST"]),
        ("/pools/set_pack", h_set_pack, ["POST"]),
        ("/pools/set_verified", h_set_verified, ["POST"]),
        ("/portraits/index", h_portraits_index, ["GET"]),
        ("/portraits/list", h_portraits_list, ["GET", "POST"]),
        ("/portraits/delete", h_portrait_delete, ["POST"]),
        # v0.21.0：软删除的恢复 / 彻底删除
        ("/portraits/restore", h_portrait_restore, ["POST"]),
        ("/portraits/delete_hard", h_portrait_delete_hard, ["POST"]),
        # v0.9.1：真·移动语义的互转（旧 /refs/bind 保留给上传/外部调用）
        ("/portraits/move_to_ref", h_portrait_move_to_ref, ["POST"]),
        ("/refs/move_to_portrait", h_ref_move_to_portrait, ["POST"]),
        ("/refs/upload", h_refs_upload, ["POST"]),
        ("/refs/list", h_refs_list, ["GET", "POST"]),
        ("/refs/orphans", h_refs_orphans, ["GET", "POST"]),
        ("/refs/orphan_delete", h_refs_orphan_delete, ["POST"]),
        ("/refs/set_draw_source", h_refs_set_draw_source, ["POST"]),
        ("/image/data", h_image_data, ["GET", "POST"]),
        ("/refs/bind", h_refs_bind, ["POST"]),
        ("/refs/unbind", h_refs_unbind, ["POST"]),
        ("/pools/set_cover", h_set_cover, ["POST"]),
        ("/works/set_cover", h_works_set_cover, ["POST"]),
        ("/jobs/start", h_job_start, ["POST"]),
        ("/jobs/status", h_job_status, ["GET", "POST"]),
        ("/jobs/archive", h_job_archive, ["POST"]),
        ("/jobs/to_ref", h_job_to_ref, ["POST"]),
        ("/records/draws", h_records_draws, ["GET", "POST"]),
        ("/records/jobs", h_records_jobs, ["GET", "POST"]),
        ("/records/stats", h_records_stats, ["GET", "POST"]),
        # v0.9.0：独立页签各自的数据出口（旧三条路由保留给老页面/外部调用）
        ("/records", h_records, ["GET", "POST"]),
        ("/records/detail", h_record_detail, ["GET", "POST"]),
        ("/records/events", h_record_events, ["GET", "POST"]),
        ("/stats", h_stats, ["GET", "POST"]),
        ("/users", h_users, ["GET", "POST"]),
        ("/user", h_user, ["GET", "POST"]),
        ("/user/avatar", h_user_avatar, ["GET", "POST"]),
        ("/filters", h_filter_options, ["GET"]),
        ("/debug/state", h_debug_state, ["GET", "POST"]),
        ("/debug/history", h_debug_history, ["GET", "POST"]),
        ("/debug/simulate", h_debug_simulate, ["POST"]),
        ("/debug/rerender", h_debug_rerender, ["POST"]),
        ("/debug/clear_draws", h_debug_clear_draws, ["POST"]),
        ("/debug/force_reroll", h_debug_force_reroll, ["POST"]),
        ("/debug/reset_char_day", h_debug_reset_char_day, ["POST"]),
        ("/debug/clear_portraits", h_debug_clear_portraits, ["POST"]),
        ("/group_pools", h_group_pools_get, ["GET"]),
        ("/group_pools", h_group_pools_set, ["POST"]),
        ("/config", h_config_get, ["GET"]),
        ("/config", h_config_set, ["POST"]),
        ("/quote_gen", h_quote_gen, ["POST"]),
        ("/ui_state", h_ui_state_get, ["GET"]),
        ("/ui_state", h_ui_state_set, ["POST"]),
        ("/overview", h_overview, ["GET", "POST"]),
    ]
    for path, fn, methods in routes:
        plugin.context.register_web_api(
            f"{ROUTE_PREFIX}{path}",
            _bind(plugin, fn),
            methods,
            f"MoeWaifu {path}",
        )
    logger.info(f"[{PLUGIN_NAME}] 已注册 {len(routes)} 条工坊路由")


def _bind(plugin, fn):
    """把 fn(plugin) 绑定成 AstrBot 可直接调用的无参协程（异常转 err 信封）。"""

    async def handler():
        try:
            return await fn(plugin)
        except Exception as e:
            logger.exception(f"[{PLUGIN_NAME}] {fn.__name__} 处理异常")
            return err(f"{fn.__name__} 失败: {e}")

    handler.__name__ = f"moe_waifu_{fn.__name__}"
    return handler
