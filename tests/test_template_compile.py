# -*- coding: utf-8 -*-
"""用页面自带的 Vue 3.5 全局构建真编译 in-DOM 模板（退出码即结论）。

`node --check` 只看 JS；`test_template_structure.py` 只看标签配对。两者都抓不到
「{{ }} 里写了非法表达式 / 指令参数写错」——Vue.compile 才会当场抛错。
不联网、不装依赖：编译器就来自 pages/waifu-console/assets/vue.global.prod.js。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "pages" / "waifu-console" / "assets"
PAGE = ROOT / "pages" / "waifu-console" / "index.html"
TMP = ROOT / "_tmp_vue_compile_check.js"

JS = r"""
const fs = require('fs');
// compiler-dom 用 document.createElement('div') 解 HTML 实体；Node 里给个最小桩，
// 只为让编译跑起来（实体解码准不准不影响语法校验）。
global.document = {
  createElement: function () {
    // Vue 解属性实体时写 `<div foo="...">` 再读 children[0].getAttribute('foo')；
    // 桩必须把原值还回去，否则所有指令表达式都会被读成空串（假报 v-if 无表达式）。
    return {
      _html: '',
      set innerHTML(v) { this._html = String(v); },
      get innerHTML() { return this._html; },
      get textContent() { return this._html; },
      get children() {
        var self = this;
        return [{ getAttribute: function () {
          var m = /<div foo="([\s\S]*)">$/.exec(self._html);
          return m ? m[1].replace(/&quot;/g, '"') : self._html;
        } }];
      }
    };
  }
};
const assetsDir = process.argv[2];
const htmlPath = process.argv[3];
const vueSrc = fs.readFileSync(assetsDir + '/vue.global.prod.js', 'utf8');
const html = fs.readFileSync(htmlPath, 'utf8');
const Vue = (new Function(vueSrc + '\nreturn Vue;'))();
// 全局构建生成的 render 代码里带 `const _Vue = Vue`，Node 里得让它可见。
global.Vue = Vue;
const start = html.indexOf('<div id="app" v-cloak>');
const marker = '</n-config-provider>';
const after = html.indexOf(marker);
const close = html.indexOf('</div>', after + marker.length);
if (start < 0 || after < 0 || close < 0) {
  console.log('FAIL: cannot locate app template boundaries');
  process.exit(1);
}
const tpl = html.slice(start, close + 6);
if (tpl.length < 1000) {
  console.log('FAIL: template suspiciously short: ' + tpl.length);
  process.exit(1);
}
try {
  Vue.compile(tpl, { onError: function (e) { throw e; } });
  console.log('ok: Vue.compile 通过（模板 ' + tpl.length + ' 字符）');
} catch (e) {
  const info = [
    e && e.code !== undefined ? 'code=' + e.code : '',
    e && e.message ? e.message : '',
    e && e.loc && e.loc.start ? 'at ' + JSON.stringify(e.loc.start) : '',
    !e || (!e.code && !e.message) ? String(e) : ''
  ].filter(Boolean).join(' | ');
  console.log('FAIL: Vue.compile 报错: ' + info);
  const ln = (e && e.loc && e.loc.start && e.loc.start.line) || 1;
  const seg = tpl.split('\n').slice(Math.max(0, ln - 3), ln + 2);
  console.log('--- template near line ' + ln + ' ---');
  seg.forEach(function (s, i) {
    console.log((Math.max(0, ln - 3) + i + 1) + ': ' + s);
  });
  process.exit(1);
}
"""


def main():
    TMP.write_text(JS, encoding="utf-8")
    try:
        r = subprocess.run(["node", str(TMP), str(ASSETS).replace("\\", "/"),
                            str(PAGE).replace("\\", "/")],
                           capture_output=True, text=True, encoding="utf-8")
        out = (r.stdout or "") + (r.stderr or "")
        print(out.strip()[:3000])
        if r.returncode != 0:
            sys.exit(1)
        if "ok: Vue.compile" not in out:
            print("FAIL: 未看到编译通过标记")
            sys.exit(1)
        print("test_template_compile: ALL OK")
    finally:
        TMP.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
