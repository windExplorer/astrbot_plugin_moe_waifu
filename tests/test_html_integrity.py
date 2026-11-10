# -*- coding: utf-8 -*-
"""工坊页面 HTML 完整性守卫（可直接运行，退出码即结论）。

背景（v0.2.3 真机事故）：Dashboard 服务端往页面注入桥标签的方式是
`html.replace("</body>", 桥标签 + "</body>", 1)` —— 取**第一个**匹配。
若页面任何位置（注释/JS 字符串）出现该闭合标签字面量，注入就会命中那里、
把主脚本拦腰截断，浏览器把剩余源码当正文渲染（「整页都是代码」）。
"""
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "pages" / "waifu-console" / "index.html"

BODY_CLOSE = "<" + "/body>"
SCRIPT_CLOSE = "<" + "/script>"
HTML_CLOSE = "<" + "/html>"


def check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    print(f"ok: {name}")


def main():
    src = io.open(PAGE, encoding="utf-8").read()

    check("body 闭合标签全文件唯一（服务端注入锚点）",
          src.count(BODY_CLOSE) == 1)
    check("html 闭合标签全文件唯一", src.count(HTML_CLOSE) == 1)

    # script 开/闭平衡：vendored 2 个（单行自闭合）+ 主脚本 1 个
    check("script 开闭平衡", src.count("<script") == src.count(SCRIPT_CLOSE) == 3)

    # 主脚本内不得有闭合标签字面量（只切主脚本自身的内容，闭合标签之后不算）
    main_start = src.rfind("<script>")
    main_end = src.find(SCRIPT_CLOSE, main_start)
    check("主脚本存在", main_start > 0 and main_end > main_start)
    main_body = src[main_start:main_end]
    check("主脚本内无 body 闭合字面量", main_body.count(BODY_CLOSE) == 0)
    check("主脚本内无 script 闭合字面量", main_body.count(SCRIPT_CLOSE) == 0)

    # 基本结构次序
    check("结构次序 script→body→html",
          src.rfind(SCRIPT_CLOSE) < src.rfind(BODY_CLOSE) < src.rfind(HTML_CLOSE))

    # 关键钩子仍在（防误删）
    for token in ("AstrBotPluginPage", "bridgeReady", "app.mount"):
        check(f"主脚本含 {token}", token in main_body)

    print("test_html_integrity: ALL OK")


if __name__ == "__main__":
    main()
