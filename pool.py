# -*- coding: utf-8 -*-
"""萌萌今日老婆 · 角色库与种子抽取（无 astrbot 依赖，可独立测试）。

设计要点（PRD §3/§4）：
- 角色库 = data/plugin_data/.../pools/ 下「一个 JSON 一卷」，data 目录是唯一真源；
  包内只带 seed_pool.json（种子卷），首启复制缺失卷，绝不覆盖已有文件。
- 抽取是种子决定论：seed = SHA-256(f"{date}|{user_id}|{salt}|{nonce}")，
  全部维度由种子派生（hashlib，禁全局 random），同一天同一人同一 nonce 结果恒定。
- 群过滤：group_pools 配置每行「群号=卷名,卷名」，抽卡只在
  「该群绑定卷 ∩ verified（或 allow_unverified）」的并集里抽。
- 候选按 (pack, id) 排序后再用种子取模——卷文件增删条目会改变全体排名，
  这是决定论与可编辑库的固有折衷（换库不追溯，当日已落库的签照旧）。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

CATEGORY_LABELS = {
    "anime": "动漫",
    "gal": "galgame",
    "game": "游戏",
    "other": "其他",
}


# ---------- 种子派生 ----------

def derive_seed(date_str: str, user_id: str, salt: str, nonce: int = 0) -> bytes:
    raw = f"{date_str}|{user_id}|{salt}|{int(nonce)}".encode("utf-8")
    return hashlib.sha256(raw).digest()


def sub_int(seed: bytes, tag: str, modulus: int) -> int:
    """从种子+维度标签派生 [0, modulus) 的整数。modulus<=0 时返回 0。"""
    if modulus <= 0:
        return 0
    digest = hashlib.sha256(seed + b"|" + tag.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % modulus


def pick_character(seed: bytes, candidates: list) -> dict:
    """按种子从候选中确定性选一位。candidates 必须已排序（见 candidates_for）。"""
    if not candidates:
        raise ValueError("候选角色为空，无法抽取")
    return candidates[sub_int(seed, "char", len(candidates))]


def portrait_seed_int(char_id: str, date_str: str, seq: int, salt: str) -> int:
    """立绘生成种子：同角色同天第 seq 张恒定（31 位内，萌绘可接受）。"""
    raw = f"portrait|{char_id}|{date_str}|{int(seq)}|{salt}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big") % (2 ** 31)


def pool_index(char_id: str, date_str: str, pool_size: int, salt: str) -> int:
    """满库后从立绘库取哪一张：同角色同天恒定 → 同群同款共享同一张图。"""
    raw = f"pick|{char_id}|{date_str}|{salt}".encode("utf-8")
    if pool_size <= 0:
        return 0
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big") % pool_size


# ---------- 群绑定 ----------

def parse_group_pools(text: str) -> dict:
    """解析「群号=卷名,卷名」多行文本 → {群号: [卷名...]}。

    同一群号出现多行时合并去重（保序）；空行与坏行跳过。
    """
    result: dict = {}
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        gid, _, packs = line.partition("=")
        gid = gid.strip()
        names = [p.strip() for p in packs.replace("，", ",").split(",") if p.strip()]
        if not gid or not names:
            continue
        merged = result.setdefault(gid, [])
        for n in names:
            if n not in merged:
                merged.append(n)
    return result


def pack_names_for_group(bound: dict, group_id: str) -> list | None:
    """群绑定卷名列表；未绑定（或私聊传空）返回 None = 全部启用卷。"""
    if not group_id:
        return None
    names = bound.get(str(group_id))
    return list(names) if names else None


# ---------- 库装载 ----------

def load_packs(pools_dir: Path, logger=None) -> list:
    """读取 pools/ 下全部卷 JSON。坏文件跳过并记日志（不阻塞其它卷）。"""
    packs: list = []
    seen: set = set()
    for path in sorted(Path(pools_dir).glob("*.json")) if Path(pools_dir).exists() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            if logger is not None:
                logger.warning(f"[moe_waifu] 角色卷解析失败，已跳过 {path.name}: {exc}")
            continue
        if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
            if logger is not None:
                logger.warning(f"[moe_waifu] 角色卷格式不对（缺 entries），已跳过 {path.name}")
            continue
        name = str(data.get("pack") or path.stem)
        pack = {
            "pack": name,
            "title": str(data.get("title") or name),
            "enabled": bool(data.get("enabled", True)),
            "entries": [e for e in data["entries"] if isinstance(e, dict)],
        }
        seen.add(name)
        packs.append(pack)
    return packs


def install_seed_pool(pools_dir: Path, bundled_json: Path, logger=None) -> list:
    """首启把包内种子卷复制到 pools/（仅写缺失的卷文件，绝不覆盖用户已有卷）。

    返回本次新装的卷名列表。
    """
    pools_dir = Path(pools_dir)
    pools_dir.mkdir(parents=True, exist_ok=True)
    installed: list = []
    try:
        data = json.loads(Path(bundled_json).read_text(encoding="utf-8"))
    except Exception as exc:
        if logger is not None:
            logger.error(f"[moe_waifu] 包内种子卷解析失败: {exc}")
        return installed
    for pack in data.get("packs", []) if isinstance(data, dict) else []:
        name = str(pack.get("pack") or "").strip()
        if not name or not isinstance(pack.get("entries"), list):
            continue
        target = pools_dir / f"{name}.json"
        if target.exists():
            continue
        try:
            target.write_text(
                json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            installed.append(name)
        except Exception as exc:
            if logger is not None:
                logger.error(f"[moe_waifu] 种子卷 {name} 写入失败: {exc}")
    if installed and logger is not None:
        logger.info(f"[moe_waifu] 已安装种子卷：{', '.join(installed)}")
    return installed


def normalize_entry(entry: dict, pack_name: str) -> dict | None:
    """条目归一化 + 必要字段校验；不合格返回 None（宁可少一个角色，不可脏数据进池）。"""
    cid = str(entry.get("id") or "").strip()
    name = str(entry.get("name") or "").strip()
    tags = str(entry.get("tags_en") or "").strip()
    if not cid or not name or not tags:
        return None
    strategy = entry.get("strategy") if isinstance(entry.get("strategy"), dict) else {}
    stype = str(strategy.get("type") or "base").strip().lower()
    if stype not in ("base", "lora", "img2img"):
        stype = "base"
    out = {
        "id": cid,
        "name": name,
        "series": str(entry.get("series") or "").strip(),
        "pack": str(entry.get("pack") or pack_name),
        "category": str(entry.get("category") or "other").strip().lower(),
        "tier": str(entry.get("tier") or "").strip().upper()[:2],
        "strategy": strategy,
        "strategy_type": stype,
        "tags_en": tags,
        "quote": str(entry.get("quote") or "").strip(),
        "color": str(entry.get("color") or "#E8B4C8").strip(),
        "verified": bool(entry.get("verified", False)),
    }
    if out["category"] not in CATEGORY_LABELS:
        out["category"] = "other"
    return out


def candidates_for(
    packs: list,
    group_id: str = "",
    group_pools_text: str = "",
    allow_unverified: bool = False,
) -> list:
    """某群当前可抽的候选：启用卷（+ 群绑定过滤）∩ verified（可放宽）→ 排序。

    排序键 (pack, id)：同一天内库不变则结果恒定；条目增删会全体重排（见模块注释）。
    """
    bound = parse_group_pools(group_pools_text)
    want = pack_names_for_group(bound, group_id)
    out: list = []
    seen_ids: set = set()
    for pack in packs:
        if want is not None and pack["pack"] not in want:
            continue
        if not pack["enabled"]:
            continue
        for raw in pack["entries"]:
            entry = normalize_entry(raw, pack["pack"])
            if entry is None:
                continue
            if not allow_unverified and not entry["verified"]:
                continue
            if entry["id"] in seen_ids:  # 跨卷重复 id：先见者胜
                continue
            seen_ids.add(entry["id"])
            out.append(entry)
    out.sort(key=lambda e: (e["pack"], e["id"]))
    return out


def find_entry(packs: list, ident: str) -> dict | None:
    """按 id 或名字找条目（试画用；不分卷、不看 verified）。"""
    ident = str(ident or "").strip().lower()
    if not ident:
        return None
    fallback = None
    for pack in packs:
        for raw in pack["entries"]:
            entry = normalize_entry(raw, pack["pack"])
            if entry is None:
                continue
            if entry["id"].lower() == ident:
                return entry
            if fallback is None and entry["name"].lower() == ident:
                fallback = entry
    return fallback


def entry_label(entry: dict) -> str:
    """卡片/日志用的短标识：名字（作品）。"""
    series = entry.get("series") or ""
    return f"{entry['name']}（{series}）" if series else entry["name"]
