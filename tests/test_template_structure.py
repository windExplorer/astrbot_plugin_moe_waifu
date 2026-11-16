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

    # v0.49.0：角色库第三视图「头像墙」——纯头像 + 文字头像兜底 + 悬停气泡
    check("角色库有头像墙视图", 'v-if="charsMode === \'avatar\'"' in page
          and ".av-wall" in page and ".av-hover" in page)
    check("头像墙有文字头像兜底", 'class="av-letter"' in page)
    # v0.63.0：点头像整块进详情（不再点头像看大图），看大图移到右上角放大镜
    avseg2 = page.split("charsMode === 'avatar'", 2)[1][:2600]
    check("头像墙点头像直接进详情", 'class="av-tile"' in avseg2
          and '@click="gotoGallery(e.id)"' in avseg2
          and 'class="av-detail"' not in avseg2)  # 旧「详情」角标已撤，整块可点
    check("头像墙看大图移到放大镜角标", 'class="av-zoom"' in avseg2
          and '@click.stop="openCharAvatarViewer(e)"' in avseg2
          and '.av-zoom' in page)
    # 详情页加载动画：旋转圈 + 页签下方进度条
    check("详情页加载有旋转圈+进度条动画", '.gal-spin' in page
          and '@keyframes gal-spin' in page and 'class="gal-loadbar"' in page
          and '@keyframes gal-loadbar' in page)
    # 顶部头像改看大图，不再跳资料 tab
    check("顶部头像点击看大图而非跳资料tab",
          '@click="openCharAvatarViewer(null)"' in page
          and '@click="switchGalTab(\'profile\')"' not in page)
    # 同一个坑：预览弹窗里有 v-if 兄弟节点（加载态 / 清单 / 提示），不能放 n-space
    avseg = page.split('title="批量头像 · 预览"', 1)[1][:400]
    check("批量头像预览容器不是 n-space（div.stack）",
          "<n-space" not in avseg and '<div class="stack">' in avseg)
    # v0.50.1：预览里有「覆盖已有头像」开关（默认值来自配置）
    check("批量头像预览有覆盖开关",
          "galAvOverwrite" in page and "setAvOverwrite" in page)

    # v0.59.3：试画台三步容器不得是 n-space。vendored naive 的 Space 给每个槽
    # 子元素包 div 且 key 全是字面量 1，tryStep 1/2/3 反复切换时 keyed diff 错位，
    # 实测第2步整块被复制 4 份。必须 div.stack（.stack > * + * 提供 12px 间距）。
    try_seg = page.split('<!-- ============ 试画台', 1)[1]
    try_seg = try_seg.split('<!-- ============ 角色详情', 1)[0]
    check("试画台步骤容器不是 n-space（div.stack）",
          '<n-space vertical size="medium">' not in try_seg
          and '<div class="stack">' in try_seg)
    check("试画台三个步骤分支都在",
          all(f'v-if="tryStep === {n}"' in try_seg for n in (1, 2, 3)))

    if FAILURES:
        print(f"test_template_structure: {len(FAILURES)} FAILED")
        sys.exit(1)
    print("test_template_structure: ALL OK")


if __name__ == "__main__":
    main()
