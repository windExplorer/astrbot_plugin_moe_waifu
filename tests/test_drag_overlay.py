# -*- coding: utf-8 -*-
"""拖拽全屏提示层（drag-overlay）守卫，可直接运行，退出码即结论。

v0.71.2：全局图库 w-upload-panel 的 onDrop 里 stopPropagation，
document 冒泡级 drop 收不到 → 遮罩卡死、跨页残留且没有关闭入口。
守卫住「捕获级统一收尾」这条兜底链，防止日后重构又复现同一个坑。
"""
import re
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

    # 1) 统一收尾函数：清零 dragDepth + 关遮罩
    m = re.search(r"function resetDragVisual\(\)\s*\{(.*?)\}", page, re.S)
    check("存在统一拖拽视觉收尾函数", m is not None)
    if m:
        body = m.group(1)
        check("收尾函数清零 dragDepth", "dragDepth = 0" in body, body)
        check("收尾函数关闭 dragOverlay", "dragOverlay.value = false" in body, body)

    # 2) 捕获级兜底：window 的 drop / dragend 都必须带 true（捕获）
    #    ——写成 false 或省略就拦不住 stopPropagation，等于白改
    check("window drop 捕获级收尾",
          bool(re.search(r'window\.addEventListener\("drop",\s*resetDragVisual,\s*true\)', page)))
    check("window dragend 捕获级收尾",
          bool(re.search(r'window\.addEventListener\("dragend",\s*resetDragVisual,\s*true\)', page)))

    # 3) dragleave 离开窗口/取消拖拽时强制收尾
    dl = re.search(r'document\.addEventListener\("dragleave",\s*function \(e\) \{(.*?)\n      \}\);', page, re.S)
    check("document dragleave 监听存在", dl is not None)
    if dl:
        check("dragleave 无 relatedTarget 时强制收尾",
              "!e.relatedTarget" in dl.group(1) and "resetDragVisual()" in dl.group(1),
              dl.group(1))

    # 4) dragenter 落在上传框时不误报"不接收文件"
    de = re.search(r'document\.addEventListener\("dragenter",\s*function \(e\) \{(.*?)\n      \}\);', page, re.S)
    check("document dragenter 监听存在", de is not None)
    if de:
        check("dragenter 识别 .dropzone", 'closest(".dropzone")' in de.group(1))
        check("上传框文案为松手上传到上传框", "松手上传到上传框" in de.group(1))
        check("非上传框仍保留不接收文件兜底文案", "当前页面不接收文件" in de.group(1))

    # 5) 原有防双传逻辑没被破坏
    dz = re.search(r"onDrop:\s*function \(e\) \{(.*?)\n      \},", page, re.S)
    check("上传框 onDrop 仍 stopPropagation（说明捕获兜底为何必要）",
          bool(dz) and "stopPropagation()" in dz.group(1))
    drop = re.search(r'document\.addEventListener\("drop",\s*function \(e\) \{(.*?)\n      \}\);', page, re.S)
    check("冒泡 drop 仍读 defaultPrevented 防双传",
          bool(drop) and "zoneHandled = e.defaultPrevented" in drop.group(1)
          and "if (zoneHandled) return" in drop.group(1))

    # 6) v0.71.6 心跳兜底：深度配平在拖拽途中 DOM 被 Vue 重渲染时会断对
    #    （元素被删 → 对应 dragleave 永远不来 → 深度恒 >0，遮罩卡死无关闭入口，
    #    真机复现）。活拖拽期间 dragover 持续高频触发，遮罩开着却 1.2s 没有
    #    dragover 必然是卡死，强制收尾自愈。
    check("dragover 心跳时间戳", "lastDragTs = Date.now()" in page)
    check("心跳检查函数超时强制收尾",
          bool(re.search(r"function dragHeartbeatCheck\(\)\s*\{.*?"
                         r"Date\.now\(\) - lastDragTs > 1200.*?resetDragVisual\(\)",
                         page, re.S)))
    de2 = re.search(r'document\.addEventListener\("dragenter",\s*function \(e\) \{(.*?)\n      \}\);', page, re.S)
    check("dragenter 点亮遮罩时布防心跳",
          bool(de2) and "armDragWatchdog()" in de2.group(1), de2.group(1) if de2 else "")
    do2 = re.search(r'document\.addEventListener\("dragover",\s*function \(e\) \{(.*?)\n      \}\);', page, re.S)
    check("dragover 持续保活心跳",
          bool(do2) and "armDragWatchdog()" in do2.group(1), do2.group(1) if do2 else "")
    check("收尾函数清掉心跳定时器",
          "clearTimeout(dragWatchdog)" in page and "dragWatchdog = null" in page)
    check("Esc 键可手动收尾", '"Escape"' in page)

    print("test_drag_overlay: " + ("ALL OK" if not FAILURES else f"{len(FAILURES)} FAILED"))
    sys.exit(0 if not FAILURES else 1)


if __name__ == "__main__":
    main()
