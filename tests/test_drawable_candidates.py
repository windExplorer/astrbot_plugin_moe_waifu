# -*- coding: utf-8 -*-
"""v0.68.1 守卫：pool 模式选角前剔除库空角色（自动换人、不画新图、不算换老婆次数）。

本机 import 不了整个 astrbot 运行时：用 AST 从 main.py 原样抠出
_drawable_candidates 定义再 exec —— 测的是磁盘上的真代码，不为跑测试去装 astrbot。
可直接运行，退出码即结论。
"""
import ast
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import store  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if not cond:
        FAILURES.append(f"{name} {detail}")
        print(f"FAIL: {name} {detail}")
    else:
        print(f"ok: {name}")


# 从 main.py 抠出 _drawable_candidates 方法（MoeWaifuPlugin 类体内）
main_src = (ROOT / "main.py").read_text(encoding="utf-8")
tree = ast.parse(main_src)
fn = None
for node in ast.walk(tree):
    if isinstance(node, ast.ClassDef) and node.name == "MoeWaifuPlugin":
        for item in node.body:
            if isinstance(item, ast.FunctionDef) and item.name == "_drawable_candidates":
                fn = item
                break
check("main.py 里能找到 _drawable_candidates 定义", fn is not None)
if fn is None:
    sys.exit(1)
mod = ast.Module(body=[fn], type_ignores=[])
ast.fix_missing_locations(mod)
ns = {}
exec(compile(mod, str(ROOT / "main.py"), "exec"), ns)
drawable = ns["_drawable_candidates"]

CANDIDATES = [
    {"id": "has1", "name": "有图甲"},
    {"id": "empty", "name": "空库乙"},
    {"id": "has2", "name": "有图丙"},
]

with tempfile.TemporaryDirectory() as td:
    st = store.Store(Path(td) / "waifu.db")
    st.add_portrait("has1", "portraits/has1/1.png", 1, "2026-11-17", 0, "base")
    st.add_portrait("has2", "portraits/has2/1.png", 1, "2026-11-17", 0, "base")
    # empty 角色一张都没有

    def make(mode):
        return SimpleNamespace(_cfg=lambda k, d=None: "pool" if k == "gen_source_mode" else d,
                               _store=st)

    got = [e["id"] for e in drawable(make("pool"), CANDIDATES)]
    check("pool 模式剔除库空角色", got == ["has1", "has2"], str(got))
    check("pool 模式不改动原候选列表（返回新列表）", len(CANDIDATES) == 3)

    def make_mode(mode):
        return SimpleNamespace(_cfg=lambda k, d=None: mode if k == "gen_source_mode" else d,
                               _store=st)

    check("mix 模式不过滤（库空角色照常可抽、会现画）",
          [e["id"] for e in drawable(make_mode("mix"), CANDIDATES)] == ["has1", "empty", "has2"])
    check("img2img 模式不过滤",
          [e["id"] for e in drawable(make_mode("img2img"), CANDIDATES)] == ["has1", "empty", "has2"])

    # 全库皆空 → 过滤后为空（上层据此给「没有任何角色有立绘」提示）
    st_empty = store.Store(Path(td) / "empty.db")
    only_empty = SimpleNamespace(_cfg=lambda k, d=None: "pool" if k == "gen_source_mode" else d,
                                 _store=st_empty)
    check("pool 模式全库无图→候选为空", drawable(only_empty, CANDIDATES) == [])

    # 无 store 时安全降级（不过滤，交给后续流程报错）
    nostore = SimpleNamespace(_cfg=lambda k, d=None: "pool" if k == "gen_source_mode" else d,
                              _store=None)
    check("无 store 安全降级不过滤", drawable(nostore, CANDIDATES) == CANDIDATES)

    # 关连接，避免 Windows 上临时目录清理撞文件锁
    st.close()
    st_empty.close()

if FAILURES:
    print(f"test_drawable_candidates: {len(FAILURES)} FAILED")
    sys.exit(1)
print("test_drawable_candidates: ALL OK")
