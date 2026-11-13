# -*- coding: utf-8 -*-
"""萌萌今日老婆 · 角色库与种子抽取（无 astrbot 依赖，可独立测试）。

设计要点（PRD §3/§4）：
- 角色库 = data/plugin_data/.../pools/ 下「一个 JSON 一卷」，data 目录是唯一真源；
  包内只带 seed_pool.json（种子卷），首启复制缺失卷，绝不覆盖已有文件。
- 抽取是种子决定论：seed = SHA-256(f"{date}|{user_id}|{salt}|{nonce}")，
  全部维度由种子派生（hashlib，禁全局 random），同一天同一人同一 nonce 结果恒定。
- 群过滤：group_pools 配置每行「群号=作品,作品」（作品即角色的 series，如
  原神/鸣潮/异环），抽卡只在该群绑定作品 ∩ verified（或 allow_unverified）里抽。
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

def derive_seed(date_str: str, user_id: str, salt: str, nonce: int = 0,
                epoch: int = 0) -> bytes:
    """抽签种子：SHA-256(f"{date}|{uid}|{salt}|{nonce}|{epoch}")。

    epoch 是「重置轮次」（store.user_epochs，调试清签时 +1）：同一天同一人
    nonce=0 的结果本恒定（一人一天一位），但用户在工坊「重置」后重新抽，
    期望换个人——epoch 入种子后，每清一次签种子就整体重派（v0.9.5）。
    """
    raw = f"{date_str}|{user_id}|{salt}|{int(nonce)}|{int(epoch)}".encode("utf-8")
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


def pick_quote(entry: dict, seed: bytes) -> str:
    """按种子从角色印象语列表里确定性选一句（纯函数，可测）。

    兼容三种数据形态：quotes 列表（多条）、quote 单条、两者皆空返回空串。
    同一签恒定；换老婆/重置后种子变了，句子也跟着换。
    """
    quotes = [str(q).strip() for q in (entry.get("quotes") or [])
              if str(q).strip()] if isinstance(entry.get("quotes"), list) else []
    if not quotes:
        quotes = [str(entry.get("quote") or "").strip()]
    quotes = [q for q in quotes if q]
    if not quotes:
        return ""
    return quotes[sub_int(seed, "quote", len(quotes))]


def pool_index(char_id: str, date_str: str, pool_size: int, salt: str) -> int:
    """满库后从立绘库取哪一张：同角色同天恒定 → 同群同款共享同一张图。"""
    raw = f"pick|{char_id}|{date_str}|{salt}".encode("utf-8")
    if pool_size <= 0:
        return 0
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big") % pool_size


# ---------- 群绑定 ----------

def parse_group_pools(text: str) -> dict:
    """解析「群号=作品,作品」多行文本 → {群号: [作品...]}。

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


def series_for_group(bound: dict, group_id: str) -> set | None:
    """群绑定的作品集合（角色 series 维度）；未绑定（或私聊传空）返回 None = 全部启用卷。"""
    if not group_id:
        return None
    names = bound.get(str(group_id))
    return set(names) if names else None


# ---------- 库装载 ----------

# v0.25.0：数据源钩子。插件初始化时把 store.load_packs 挂上来，所有读路径
# 自动走 SQLite 新表；新表为空 / 抛异常时回退卷 JSON——地基出问题绝不能让
# 抽卡没候选。写路径不动（JSON 仍是真源，写完由 webui_api 同步新表）。
_PACKS_PROVIDER = None


def set_packs_provider(fn) -> None:
    """设置卷数据源（None = 只用 JSON）。main.py 初始化时挂 store 读方法。"""
    global _PACKS_PROVIDER
    _PACKS_PROVIDER = fn


def load_packs(pools_dir: Path, logger=None) -> list:
    """读取角色卷：优先新表（v0.25.0），空表 / 异常回退 pools/*.json。"""
    if _PACKS_PROVIDER is not None:
        try:
            packs = _PACKS_PROVIDER()
            if packs:
                return packs
        except Exception as exc:  # 地基坏了也不能阻塞抽卡
            if logger is not None:
                logger.warning(f"[moe_waifu] 新表读取失败，回退卷 JSON: {exc}")
    return _load_packs_json(pools_dir, logger)


def _load_packs_json(pools_dir: Path, logger=None) -> list:
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
    """把包内种子卷落到 pools/：缺失的卷整卷安装；已存在的卷按 id 合并新条目。

    合并语义（v0.3.1）：种子卷里 id 在用户卷中不存在 → 追加（verified 默认 false，
    走试画验证流程）；id 已存在 → 一律以用户卷为准，**绝不覆盖**——
    data 目录是唯一真源，种子卷只是播种机。
    返回 {"installed": [新装卷名], "merged": {卷名: 追加条数}}。
    """
    pools_dir = Path(pools_dir)
    pools_dir.mkdir(parents=True, exist_ok=True)
    installed: list = []
    merged: dict = {}
    try:
        data = json.loads(Path(bundled_json).read_text(encoding="utf-8"))
    except Exception as exc:
        if logger is not None:
            logger.error(f"[moe_waifu] 包内种子卷解析失败: {exc}")
        return {"installed": installed, "merged": merged}
    for pack in data.get("packs", []) if isinstance(data, dict) else []:
        name = str(pack.get("pack") or "").strip()
        if not name or not isinstance(pack.get("entries"), list):
            continue
        target = pools_dir / f"{name}.json"
        if not target.exists():
            try:
                target.write_text(
                    json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                installed.append(name)
            except Exception as exc:
                if logger is not None:
                    logger.error(f"[moe_waifu] 种子卷 {name} 写入失败: {exc}")
            continue
        # 已有卷：按 id 合并种子卷里新增的条目（不覆盖用户已有条目）
        try:
            current = json.loads(target.read_text(encoding="utf-8"))
        except Exception as exc:
            if logger is not None:
                logger.warning(f"[moe_waifu] 卷 {name} 解析失败，跳过合并: {exc}")
            continue
        entries = current.get("entries") if isinstance(current.get("entries"), list) else []
        have_ids = set()
        for raw in entries:
            e = normalize_entry(raw, name)
            if e:
                have_ids.add(e["id"])
        appended = 0
        for raw in pack["entries"]:
            e = normalize_entry(raw, name)
            if e and e["id"] not in have_ids:
                entries.append(e)
                have_ids.add(e["id"])
                appended += 1
        if appended:
            current["entries"] = entries
            try:
                target.write_text(
                    json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                merged[name] = appended
            except Exception as exc:
                if logger is not None:
                    logger.error(f"[moe_waifu] 卷 {name} 合并写回失败: {exc}")
    if installed and logger is not None:
        logger.info(f"[moe_waifu] 已安装种子卷：{', '.join(installed)}")
    if merged and logger is not None:
        logger.info(
            "[moe_waifu] 种子卷合并："
            + ", ".join(f"{k}+{v}" for k, v in merged.items())
        )
    return {"installed": installed, "merged": merged}


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
    # 印象语（v0.9.5）：quotes 多条列表（卡片按种子随机显示一条）；
    # 兼容旧的单条 quote 字段（并入列表首位，不丢数据）。
    quotes = []
    if isinstance(entry.get("quotes"), list):
        quotes = [str(q).strip() for q in entry["quotes"] if str(q).strip()]
    single = str(entry.get("quote") or "").strip()
    if single and single not in quotes:
        quotes.insert(0, single)
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
        "quote": single or (quotes[0] if quotes else ""),
        "quotes": quotes,
        "color": str(entry.get("color") or "#E8B4C8").strip(),
        "cover": str(entry.get("cover") or "").strip(),   # 角色封面（v0.5.0）
        "verified": bool(entry.get("verified", False)),
    }
    if out["category"] not in CATEGORY_LABELS:
        out["category"] = "other"
    return out


def merge_saved_entry(raw: dict, normalized: dict) -> dict:
    """编辑保存时合并旧条目：只覆盖表单实际提交的字段，其余原样保留。

    `h_save_entry` 是整体替换（`entries[i] = normalized`），而工坊表单会
    **重建** strategy（只有 type，img2img 时才带 ref / denoise），既不带
    `strategy.refs`（参考图库）也不带 `cover`。不做这层合并，用户「改个
    名字点保存」就会把该角色全部参考图绑定、主参考与封面清空——文件还在
    data/refs/，工坊里却一张都看不到了（v0.9.10 实测事故）。

    strategy 只 `update` 本次提交的键、绝不主动 pop：base/lora 模式下表单
    不带 ref / loras / denoise，旧值一律留着（对当前画法无副作用，切回
    img2img 还能继续用），比"顺手清掉"安全得多。
    """
    merged = dict(raw) if isinstance(raw, dict) else {}
    for key, value in (normalized or {}).items():
        if key == "strategy":
            old_st = merged.get("strategy")
            st = dict(old_st) if isinstance(old_st, dict) else {}
            st.update(value or {})
            merged["strategy"] = st
        elif key != "cover":
            # cover 不在编辑表单里，旧值必须保住
            merged[key] = value
    return merged


# ---------- 导入覆盖 / 重复角色（v0.23.0） ----------

# 导入「覆盖」只允许改这些基础字段。strategy（参考图 ref/refs/gen_refs、
# 画法、工作流）、cover（封面）、verified（本地验证状态）、id/pack 一律保留
# 本地值——立绘在 portraits 表按 id 关联，参考图绑定在 strategy 里，
# 整体替换条目会让这些资产在界面上凭空消失（v0.9.10 同类事故）。
IMPORT_BASIC_KEYS = ("name", "series", "category", "tier", "tags_en",
                     "quote", "quotes", "color")


def merge_imported_entry(raw: dict, imported: dict) -> dict:
    """导入覆盖：只更新基础信息，资产字段原样保留。纯函数。"""
    out = dict(raw) if isinstance(raw, dict) else {}
    for key in IMPORT_BASIC_KEYS:
        if key in (imported or {}):
            out[key] = imported[key]
    return out


def ref_paths(entry: dict) -> list:
    """条目上绑定的全部参考图路径（refs 资料库 + gen_refs 图生图源 + ref 主参考）。"""
    st = entry.get("strategy") if isinstance(entry.get("strategy"), dict) else {}
    out: list = []
    for key in ("ref", "refs", "gen_refs"):
        val = st.get(key)
        for x in (val if isinstance(val, list) else [val]):
            s = str(x or "").strip()
            if s and s not in out:
                out.append(s)
    return out


def asset_score(entry: dict, portrait_count: int = 0) -> int:
    """资产丰度：立绘 >> 参考图 > 封面 > 已验证。去重时保留分数最高的一条。"""
    cover = str((entry or {}).get("cover") or "").strip()
    return (int(portrait_count or 0) * 1000
            + len(ref_paths(entry or {})) * 10
            + (5 if cover else 0)
            + (1 if (entry or {}).get("verified") else 0))


def merge_entry_assets(base: dict, extra: dict) -> dict:
    """把 extra 的参考图 / 封面 / 验证状态并入 base（只增不删）。纯函数。

    用于去重：被移除条目的绑定不能跟着丢，合到保留条目上；图片文件本身
    一个都不删（立绘在 portraits 表按 id 关联，由调用方迁移归属）。
    """
    out = dict(base) if isinstance(base, dict) else {}
    bst = dict(out.get("strategy") or {}) if isinstance(out.get("strategy"), dict) else {}
    est = (extra or {}).get("strategy") if isinstance((extra or {}).get("strategy"), dict) else {}
    b_refs = [str(x) for x in (bst.get("refs") or []) if str(x or "").strip()]
    for x in (est.get("refs") or []):
        s = str(x or "").strip()
        if s and s not in b_refs:
            b_refs.append(s)
    if b_refs:
        bst["refs"] = b_refs
    b_gen = [str(x) for x in (bst.get("gen_refs") or []) if str(x or "").strip()]
    for x in (est.get("gen_refs") or []):
        s = str(x or "").strip()
        if s and s not in b_gen:
            b_gen.append(s)
    if b_gen:
        bst["gen_refs"] = b_gen
    if not str(bst.get("ref") or "").strip() and str(est.get("ref") or "").strip():
        bst["ref"] = est["ref"]
    out["strategy"] = bst
    if not str(out.get("cover") or "").strip() and str((extra or {}).get("cover") or "").strip():
        out["cover"] = extra["cover"]
    if (extra or {}).get("verified"):
        out["verified"] = True
    return out


def duplicate_groups(packs: list) -> list:
    """并查集扫描全库重复：同 id，或同 (name, series) 视作一组。

    一个条目只属于一个组（两种判重条件会连通，避免「按 id 清完又按名字
    清一遍」把同一条处理两次）。返回
    [{"key", "reasons": [...], "items": [{pack, index, raw, entry}]}]，
    只含成员数 >= 2 的组；顺序确定（按 pack/index，组按 key）。纯函数。
    """
    items: list = []
    for p in packs or []:
        pack_name = str(p.get("pack") or "")
        for idx, raw in enumerate(p.get("entries") or []):
            e = normalize_entry(raw, pack_name)
            if e is None:
                continue
            items.append({"pack": pack_name, "index": idx,
                          "raw": raw, "entry": e})
    n = len(items)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    by_id: dict = {}
    by_ns: dict = {}
    for i, it in enumerate(items):
        e = it["entry"]
        if e["id"] in by_id:
            union(i, by_id[e["id"]])
        else:
            by_id[e["id"]] = i
        ns = (str(e.get("name") or "").strip().lower(),
              str(e.get("series") or "").strip().lower())
        if ns[0]:
            if ns in by_ns:
                union(i, by_ns[ns])
            else:
                by_ns[ns] = i
    buckets: dict = {}
    for i, it in enumerate(items):
        buckets.setdefault(find(i), []).append(it)
    groups = []
    for members in buckets.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda m: (m["pack"], m["index"]))
        ids = {m["entry"]["id"] for m in members}
        names = {(str(m["entry"]["name"]).strip().lower(),
                  str(m["entry"]["series"] or "").strip().lower())
                 for m in members}
        reasons = []
        if len(ids) < len(members):
            reasons.append("同 id")
        if len(names) < len(members):
            reasons.append("同名同作品")
        first = members[0]["entry"]
        key = f"{first['id']}|{first['name']}|{first['series']}"
        groups.append({"key": key, "reasons": reasons or ["关联重复"],
                       "items": members})
    groups.sort(key=lambda g: g["key"])
    return groups


def candidates_for(
    packs: list,
    group_id: str = "",
    group_pools_text: str = "",
    allow_unverified: bool = False,
) -> list:
    """某群当前可抽的候选：启用卷（+ 群绑定作品过滤）∩ verified（可放宽）→ 排序。

    群绑定按「作品（series）」维度：绑定了作品的群只抽到对应 series 的角色，
    未绑定的群 / 私聊用全部启用卷。排序键 (pack, id)：同一天内库不变则结果恒定。
    """
    bound = parse_group_pools(group_pools_text)
    want = series_for_group(bound, group_id)  # 作品集合或 None
    out: list = []
    seen_ids: set = set()
    for pack in packs:
        if not pack["enabled"]:
            continue
        for raw in pack["entries"]:
            entry = normalize_entry(raw, pack["pack"])
            if entry is None:
                continue
            if not allow_unverified and not entry["verified"]:
                continue
            if want is not None and (entry.get("series") or "") not in want:
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
