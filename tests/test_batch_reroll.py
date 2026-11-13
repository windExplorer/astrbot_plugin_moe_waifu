# -*- coding: utf-8 -*-
"""批量补图「单个角色换一个」自检（可直接运行，退出码即结论）。

守卫点：
① `char_salt` 确定性、nonce=0 不改行为、不同 nonce 派生不同盐；
② 覆盖只作用于指定角色，别的角色主题序列不受影响；
③ 同一个 nonce 下，**预览接口返回的主题 = 启动时 build_tasks 算的主题**
   （这条最关键——否则界面写着换过了、实跑又按原种子重放）。
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import batch_fill  # noqa: E402
import prompt_gen  # noqa: E402
import webui_api  # noqa: E402

SALT = "moe-waifu"
DATE = "2026-11-14"


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL: {name} {detail}")
        sys.exit(1)
    print(f"ok: {name}")


async def _call(handler, plugin, body):
    async def fake_body():
        return body
    real = webui_api._body
    webui_api._body = fake_body
    try:
        return await handler(plugin)
    finally:
        webui_api._body = real


class P:
    """h_batch_reroll 只用 salt / today 两个配置入口。"""
    _store = object()

    def _cfg(self, key, default=None):
        return {"salt": SALT}.get(key, default)

    def _today(self):
        return DATE


def _cands(*ids, need=3):
    return [{"entry": {"id": cid, "name": cid, "series": "原神", "tags_en": "1girl"},
             "need": need, "have": 0, "used_themes": []}
            for cid in ids]


# ① char_salt
check("nonce=0 用原盐（默认行为不变）",
      batch_fill.char_salt(SALT, "ganyu", 0) == SALT)
check("同参数派生盐稳定",
      batch_fill.char_salt(SALT, "ganyu", 1) == batch_fill.char_salt(SALT, "ganyu", 1))
check("不同 nonce 派生不同盐",
      batch_fill.char_salt(SALT, "ganyu", 1) != batch_fill.char_salt(SALT, "ganyu", 2))
check("不同角色派生不同盐",
      batch_fill.char_salt(SALT, "ganyu", 1) != batch_fill.char_salt(SALT, "keqing", 1))

# ② 覆盖能换出不同序列，且只影响目标角色
_labels = {t["id"]: t["label"] for t in prompt_gen.THEMES}
_salt = SALT
_base = [t["theme_id"] for t in
         batch_fill.build_tasks(_cands("ganyu", "keqing"), _salt, date=DATE)]
_variants = set()
for n in range(1, 7):
    _variants.add(tuple(t["theme_id"] for t in batch_fill.build_tasks(
        _cands("ganyu", "keqing"), _salt, date=DATE, overrides={"ganyu": n})))
check("多次「换一个」能拿到多种不同序列", len(_variants) >= 3,
      f"{len(_variants)} 种")
_ov = [t for t in batch_fill.build_tasks(
    _cands("ganyu", "keqing"), _salt, date=DATE, overrides={"ganyu": 2})
    if t["char_id"] == "keqing"]
_keqing_base = [t for t in batch_fill.build_tasks(
    _cands("ganyu", "keqing"), _salt, date=DATE) if t["char_id"] == "keqing"]
check("覆盖只作用于指定角色",
      [t["theme_id"] for t in _ov] == [t["theme_id"] for t in _keqing_base])

# ③ 预览 = 实跑（同一个 nonce）
for nonce in (1, 2, 3):
    d = asyncio.run(_call(webui_api.h_batch_reroll, P(),
                          {"char_id": "ganyu", "need": 3, "have": 0,
                           "nonce": nonce, "used_themes": []}))["data"]
    real = [t["theme_id"] for t in batch_fill.build_tasks(
        _cands("ganyu"), _salt, date=DATE, overrides={"ganyu": nonce})]
    check(f"nonce={nonce} 预览与实跑一致",
          d["themes"] == [_labels[t] for t in real], str(d))
    check(f"nonce={nonce} 回传 themes_text",
          d["themes_text"] == " / ".join(d["themes"]), d["themes_text"])

# ④ 参数校验
check("缺 char_id 被拒",
      asyncio.run(_call(webui_api.h_batch_reroll, P(), {}))["status"] == "error")

print("test_batch_reroll: ALL OK")
