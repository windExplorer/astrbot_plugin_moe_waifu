# -*- coding: utf-8 -*-
"""v0.71.0 移动端适配守卫（可直接运行，退出码即结论）。

工坊页是单文件 Vue3 应用，样式全写在 index.html 的 <style> 里。历史版本
一条媒体查询都没有：176px 固定侧栏在手机上吃掉近一半宽度、n-grid 写死
4/6 列、nowrap 表格撑破卡片、hover 才出现的操作条在触屏上永远看不到。
这里守住移动端那套契约，防止后续改动把适配悄悄改回去。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "pages" / "waifu-console" / "index.html"
FAILURES = []


def check(name, cond, detail=""):
    if not cond:
        FAILURES.append(name)
        print(f"FAIL: {name} {detail}")
    else:
        print(f"ok: {name}")


def main():
    page = PAGE.read_text(encoding="utf-8")

    check("存在窄屏媒体查询", "@media (max-width: 768px)" in page)
    check("存在超窄屏媒体查询", "@media (max-width: 420px)" in page)
    check("动态视口高度（避开手机地址栏裁切）", "100dvh" in page)

    # 页签条：纯 div 按钮，不碰 naive 的 n-menu / n-space（keyed diff 老坑）
    nav = page.split('class="m-nav">', 1)
    check("移动端页签条存在", len(nav) > 1)
    if len(nav) > 1:
        seg = nav[1].split("</div>", 1)[0]
        check("页签条遍历 menuOpts", 'v-for="m in menuOpts"' in seg)
        check("页签条点击走 onMenu", "@click=\"onMenu(m.key)\"" in seg)
        check("页签条是高亮按钮", ":class=\"{ on: menu === m.key }\"" in seg)
    check("页签条默认隐藏（桌面不受影响）", ".m-nav { display: none; }" in page)
    check("窄屏隐藏侧栏", ".n-layout-sider { display: none !important; }" in page)
    check("窄屏页签条改为横向滚动", "-webkit-overflow-scrolling: touch" in page
          and ".m-nav::-webkit-scrollbar" in page)

    # 切页后当前页签滚到可视中央（14 个页签必然超出一屏）
    check("页签自动滚动函数存在", "function ensureMobileTabVisible()" in page)
    check("切页监听页签滚动", "Vue.watch(menu, function () { Vue.nextTick(ensureMobileTabVisible); })" in page)

    # 网格 / 表格 / 弹窗：naive 把列数和宽度写在 inline style，必须 !important
    check("窄屏网格改两列（!important 覆盖 inline）",
          ".n-grid, .ov-grid { grid-template-columns: repeat(2, minmax(0, 1fr)) !important; }" in page)
    check("瀑布流窄屏降列", ".gr-waterfall { column-count: 2 !important;" in page)
    check("表格窄屏横滚不撑破卡片",
          ".rec-table, .kv-table { display: block; width: 100%; overflow-x: auto;" in page)
    check("抽屉窄屏全宽", ".n-drawer { width: 100vw !important;" in page)
    check("弹窗窄屏限宽", ".n-modal, .n-dialog { max-width: calc(100vw - 20px) !important; }" in page)

    # 触屏没有 hover：图片操作条 / 角标常驻
    check("触屏操作条常驻", ".gal-ops, .cover-zoom, .av-zoom, .work-hint { opacity: 1; }" in page)

    # 固定头负 margin 必须与 page-scroll 内边距同步收窄
    check("窄屏内容区内边距收紧", ".page-scroll { padding: 12px 12px 16px;" in page)
    check("窄屏 sticky 头负 margin 同步", ".sticky-head { margin: -12px -12px 18px;" in page)

    if FAILURES:
        print(f"test_mobile_responsive: {len(FAILURES)} FAILED")
        sys.exit(1)
    print("test_mobile_responsive: ALL OK")


if __name__ == "__main__":
    main()
