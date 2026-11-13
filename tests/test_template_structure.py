# -*- coding: utf-8 -*-
"""in-DOM 模板标签配对守卫（可直接运行，退出码即结论）。

`node --check` 只校验 JS，模板是 HTML：漏一个 </div> 会让 Vue 把后面的页签
当成上一个 div 的子节点，页面看着「少了一块」但不报错。这里用引号感知的
扫描器找标签（属性值里可能有 `>`，如 v-if="recTotal > recSize"，朴素正则会误判），
再用栈核对配对。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "pages" / "waifu-console" / "index.html"
VOID = {"meta", "link", "br", "hr", "img", "input", "source", "area",
        "base", "col", "embed", "param", "track", "wbr"}
FAILURES = []


def check(name, cond, detail=""):
    if not cond:
        FAILURES.append(name)
        print(f"FAIL: {name} {detail}")
    else:
        print(f"ok: {name}")


def scan_tags(body: str):
    """返回 [(closing: bool, name: str, offset: int)]，跳过注释与属性值里的尖括号。"""
    out = []
    i = 0
    n = len(body)
    while i < n:
        if body.startswith("<!--", i):
            end = body.find("-->", i + 4)
            i = n if end < 0 else end + 3
            continue
        if body[i] != "<":
            i += 1
            continue
        j = i + 1
        quote = ""
        while j < n:
            ch = body[j]
            if quote:
                if ch == quote:
                    quote = ""
            elif ch in ('"', "'"):
                quote = ch
            elif ch == ">":
                break
            j += 1
        if j >= n:
            break
        raw = body[i + 1:j]
        m = re.match(r"(/?)\s*([a-zA-Z][\w:-]*)", raw)
        if m:
            out.append((m.group(1) == "/", m.group(2).lower(),
                        raw.rstrip().endswith("/"), i))
        i = j + 1
    return out


def main():
    page = PAGE.read_text(encoding="utf-8")
    body = page.split("<body>", 1)[1].split('<script src="assets/vue.global.prod.js">', 1)[0]
    tags = scan_tags(body)
    check("模板区解析出标签", len(tags) > 200, f"共 {len(tags)} 个")

    stack, errors = [], []
    for closing, name, self_closing, offset in tags:
        if name in VOID or self_closing:
            continue
        if not closing:
            stack.append((name, offset))
        else:
            if not stack:
                errors.append(f"多余闭合 </{name}> @{offset}")
            elif stack[-1][0] != name:
                top, at = stack[-1]
                errors.append(f"闭合错配：期望 </{top}>(@{at})，实际 </{name}> @{offset}")
                # 容错恢复：往上找同名，避免一个错配刷屏
                for k in range(len(stack) - 1, -1, -1):
                    if stack[k][0] == name:
                        del stack[k:]
                        break
            else:
                stack.pop()
    if stack:
        errors.append("未闭合：" + ", ".join(f"<{n}>@{o}" for n, o in stack[-8:]))
    check("模板标签全部正确配对", not errors, "; ".join(errors[:6]))

    # 四个页签容器都在，且各自独立（不是嵌套在别的页签里）
    for key in ("records", "stats", "users", "debug"):
        check(f"页签容器 menu === '{key}' 存在",
              f'v-if="menu === \'{key}\'"' in page)
    check("旧的合并页签已移除", "记录·调试" not in page.split("<body>")[1][:200])

    # v0.9.8 回归守卫：带 v-if 切换的容器绝不能是 n-space——vendored naive 的
    # Space 给每个槽子元素包一层 div 且 key 全是 1，v-if 切换时 keyed diff 错位
    # 补丁，jsdom 真页面实证：「分群绑定」点一次出两份表单、切到别的页签还残留。
    # 页面主容器与编辑抽屉表单容器一律 div.stack。
    seg = page.split('page-scroll">', 1)[1][:400]
    check("页面主容器不是 n-space", "<n-space" not in seg)
    check("页面主容器用 div.stack", '<div class="stack">' in seg)
    dseg = page.split('<n-drawer-content :title="drawerTitle" closable>', 1)[1][:300]
    check("编辑抽屉表单容器不是 n-space（div.stack）", "<n-space" not in dseg.split("<n-grid")[0])

    # v0.18.2：角色详情「切角色」的载入态（立绘区 + 参考图区各一块骨架）。
    # 旧版切换后仍挂着上一位角色的图直到数据到达（1~3 秒），观感像点了没反应。
    check("角色详情有切换载入态（galSwitching）",
          page.count('v-if="galSwitching"') >= 2)
    check("载入态有骨架样式（gal-skel）", ".gal-skel" in page)

    if FAILURES:
        print(f"test_template_structure: {len(FAILURES)} FAILED")
        sys.exit(1)
    print("test_template_structure: ALL OK")


if __name__ == "__main__":
    main()
