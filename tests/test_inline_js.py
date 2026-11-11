# -*- coding: utf-8 -*-
"""抽出 index.html 内联主脚本 → 临时 js 文件，交给 node --check 语法校验。"""
import re
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
html = (root / "pages/waifu-console/index.html").read_text(encoding="utf-8")
scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
if len(scripts) != 1:
    print(f"FAIL: 期望 1 段内联脚本，实际 {len(scripts)}")
    sys.exit(1)
tmp = root / "_tmp_check.js"
tmp.write_text(scripts[0], encoding="utf-8")
r = subprocess.run(["node", "--check", str(tmp)], capture_output=True, text=True)
tmp.unlink(missing_ok=True)
if r.returncode != 0:
    print("FAIL: node --check\n" + r.stderr[:3000])
    sys.exit(1)
print("ok: 内联脚本 node --check 通过")
print("check_inline_js: OK")
