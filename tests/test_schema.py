# -*- coding: utf-8 -*-
"""_conf_schema.json 装载期守卫（可直接运行，退出码即结论）。

AstrBot 装载插件时 _config_schema_to_default_config 会对每个节点取 v["type"]
（astrbot/core/config/astrbot_config.py），任何节点缺 type 或 type 不在
DEFAULT_VALUE_MAP 里 → 整个插件装不上（报「加载插件…原因：'type'」）。
规则：每节点必须有 type ∈ 白名单；"object" 节点递归校验其 items 的子节点。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWED = {"int", "float", "bool", "string", "text", "list", "file",
           "object", "template_list", "dict"}


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL: {name} {detail}")
        sys.exit(1)
    print(f"ok: {name}")


def walk(schema: dict, path: str = "") -> list:
    bad = []
    for key, node in schema.items():
        where = f"{path}.{key}" if path else key
        if not isinstance(node, dict):
            bad.append(f"{where}（非对象节点）")
            continue
        t = node.get("type")
        if t not in ALLOWED:
            bad.append(f"{where}（type 缺失或非法：{t!r}）")
            continue
        if t == "object" and isinstance(node.get("items"), dict):
            bad += walk(node["items"], where)
    return bad


schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
check("schema 读取成功", isinstance(schema, dict) and len(schema) > 0,
      f"共 {len(schema)} 个顶层键")
bad = walk(schema)
check("每节点 type 合法（含 object 递归）", not bad, "; ".join(bad))

new_keys = {"prompt_llm_enabled", "prompt_llm_provider",
            "prompt_llm_timeout", "variations_enabled"}
check("v0.5.6 提示词/变化配置键齐全", new_keys.issubset(schema.keys()),
      f"缺：{sorted(new_keys - set(schema.keys()))}")
check("提示词开关默认开启", schema.get("prompt_llm_enabled", {}).get("default") is True)
check("提供商默认留空", schema.get("prompt_llm_provider", {}).get("default") == "")

print("test_schema: ALL OK")
