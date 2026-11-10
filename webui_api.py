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
import io
import re
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
    from . import pool, draw
except ImportError:  # pragma: no cover
    import pool  # type: ignore
    import draw  # type: ignore

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


def _version_of(plugin) -> str:
    try:
        return str(plugin._plugin_version()) if hasattr(plugin, "_plugin_version") else ""
    except Exception:
        return ""


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
    out_packs = []
    for pack in packs:
        entries = []
        for raw in pack["entries"]:
            entry = pool.normalize_entry(raw, pack["pack"])
            if entry is None:
                continue
            if entry["id"] not in counts:
                counts[entry["id"]] = plugin._store.count_portraits(entry["id"]) if plugin._store else 0
            entries.append(entry)
        out_packs.append({"pack": pack["pack"], "title": pack["title"],
                          "enabled": pack["enabled"], "entries": entries})
    anima_ready = draw._get_tool_handler(plugin.context, "comfyui_draw") is not None
    return ok({
        "packs": out_packs,
        "counts": counts,
        "version": _version_of(plugin),
        "anima_ready": anima_ready,
        "quota": int(plugin._cfg("portraits_per_char", 5)),
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
            entries[i] = normalized
            replaced = True
            break
    if not replaced:
        entries.append(normalized)
    data["entries"] = entries
    _json_save(path, data)
    return ok({"replaced": replaced, "entry": normalized})


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
            "thumb": _thumb_data_url(str(plugin._data_dir / first["file"]), 160) if first else None,
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
    for row in plugin._store.list_portraits(cid):
        full = Path(plugin._data_dir) / row["file"]
        items.append({
            "file": row["file"], "seq": row["seq"], "date": row["date"],
            "seed": row["seed"], "exists": full.is_file(),
            "thumb": _thumb_data_url(str(full), 220) if full.is_file() else None,
        })
    return ok({"char_id": cid, "items": items})


def _safe_portrait_rel(rel: str) -> bool:
    """立绘相对路径白名单形状：portraits/<char_id>/<file>.png，禁路径穿越。"""
    if not re.fullmatch(r"portraits/[^/\\]+/[^/\\]+", str(rel or "").replace("\\", "/")):
        return False
    return ".." not in rel


async def h_portrait_delete(plugin) -> dict:
    body = await _body()
    rel = str(body.get("file") or "").strip()
    if not _safe_portrait_rel(rel):
        return err("非法路径")
    full = Path(plugin._data_dir) / rel
    try:
        full.unlink(missing_ok=True)
    except Exception as exc:
        return err(f"删除文件失败：{exc}")
    plugin._store.delete_portrait(rel)
    return ok({"deleted": rel})


# ---------- 试画 / 补图（后台 job + 轮询） ----------

async def h_job_start(plugin) -> dict:
    body = await _body()
    kind = str(body.get("kind") or "try").strip()
    cid = str(body.get("id") or "").strip()
    if kind not in ("try", "refill"):
        return err("kind 只能是 try / refill")
    entry = pool.find_entry(pool.load_packs(_pools_dir(plugin), logger), cid)
    if entry is None:
        return err(f"角色库里找不到「{cid}」")
    if not plugin._store:
        return err("插件未初始化完成")
    job_id = f"{kind}:{cid}:{int(time.time() * 1000)}"
    plugin._jobs[job_id] = {"status": "running", "kind": kind, "char_id": cid,
                            "entry_label": pool.entry_label(entry), "src_path": ""}
    _prune_jobs(plugin)
    loop = asyncio.get_running_loop()
    loop.create_task(_run_job(plugin, job_id, entry, kind))
    return ok({"job_id": job_id, "label": pool.entry_label(entry)})


def _prune_jobs(plugin) -> None:
    jobs = plugin._jobs
    if len(jobs) <= _JOB_KEEP:
        return
    for jid in sorted(jobs, key=lambda k: k.rsplit(":", 1)[-1])[: max(0, len(jobs) - _JOB_KEEP)]:
        if jobs[jid].get("status") != "running":
            jobs.pop(jid, None)


async def _run_job(plugin, job_id: str, entry: dict, kind: str) -> None:
    job = plugin._jobs.get(job_id, {})
    char_id = entry["id"]
    date = plugin._today()
    salt = str(plugin._cfg("salt", "moe-waifu"))
    seq = plugin._store.count_portraits(char_id)
    seed_int = pool.portrait_seed_int(char_id, date, seq, salt)
    t0 = time.monotonic()
    src, status, error, _dur = await draw.generate_portrait(
        plugin.context, plugin._draw_cfg(), entry, seed_int, _synthetic_event()
    )
    duration = int((time.monotonic() - t0) * 1000)
    plugin._store.log_job(char_id, date, "try" if kind == "try" else "refill", status,
                          "workshop", entry["strategy_type"], seed_int, error, duration, src or "")
    job.update({"status": status, "error": error, "duration_ms": duration,
                "seed": seed_int, "src_path": src or "", "char_id": char_id})
    if src and kind == "refill":
        rel = draw.archive_portrait(src, plugin._data_dir, char_id, date, seq, seed_int)
        plugin._store.add_portrait(char_id, rel, seed_int, date, seq, entry["strategy_type"])
        job["archived"] = rel
    if src:
        job["preview"] = _thumb_data_url(src, 420)
    plugin._jobs[job_id] = job


async def h_job_status(plugin) -> dict:
    body = await _body()
    jid = str(body.get("job_id") or _q("job_id", "") or "").strip()
    job = plugin._jobs.get(jid)
    if job is None:
        return err("job 不存在（可能已过期）")
    out = {k: v for k, v in job.items() if k != "src_path"}
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
    seq = plugin._store.count_portraits(cid)
    rel = draw.archive_portrait(job["src_path"], plugin._data_dir, cid, date, seq,
                                int(job.get("seed") or 0))
    plugin._store.add_portrait(cid, rel, int(job.get("seed") or 0), date, seq, "try")
    job["archived"] = rel
    return ok({"archived": rel})


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
    return ok({"items": items, "version": _version_of(plugin)})


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


# ---------- 注册 ----------

def register(plugin) -> None:
    routes = [
        ("/pools", h_pools, ["GET"]),
        ("/pools/save_entry", h_save_entry, ["POST"]),
        ("/pools/delete_entry", h_delete_entry, ["POST"]),
        ("/pools/set_pack", h_set_pack, ["POST"]),
        ("/pools/set_verified", h_set_verified, ["POST"]),
        ("/portraits/index", h_portraits_index, ["GET"]),
        ("/portraits/list", h_portraits_list, ["GET", "POST"]),
        ("/portraits/delete", h_portrait_delete, ["POST"]),
        ("/jobs/start", h_job_start, ["POST"]),
        ("/jobs/status", h_job_status, ["GET", "POST"]),
        ("/jobs/archive", h_job_archive, ["POST"]),
        ("/group_pools", h_group_pools_get, ["GET"]),
        ("/group_pools", h_group_pools_set, ["POST"]),
        ("/config", h_config_get, ["GET"]),
        ("/config", h_config_set, ["POST"]),
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
