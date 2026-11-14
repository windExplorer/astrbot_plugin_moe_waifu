# -*- coding: utf-8 -*-
"""主题下拉 / 批量补图轮换池守卫（抽出页面内联 JS 交给 node 真跑）。

v0.41.0 两处回归，静态正则看不出来，必须跑页面里的真函数：
  ① 页面 vendored 的 naive-ui 不支持 type:"group" 表头（选项会被拍平显示、
     没有分组标题）。下拉必须平铺，且后端 THEMES 里一个主题都不能少；
  ② fillThemePool 只遍历顶层时，组项没有 value、而「画风」整组都在
     children 里 → 补齐配额的「换一个 / 整体重随机」里画风主题全消失。
     池子必须含全部 art_、与后端 content_theme_ids 同口径，即使喂回分组
     结构也不能漏。
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "pages" / "waifu-console" / "index.html"
sys.path.insert(0, str(ROOT))

import prompt_gen  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if not cond:
        FAILURES.append(name)
        print(f"FAIL: {name} {detail}")
    else:
        print(f"ok: {name}")


def slice_between(page, start_marker, end_marker):
    a = page.index(start_marker)
    b = page.index(end_marker, a)
    return page[a:b]


JS_TAIL = r"""
const __themes = __THEME_JSON__;
themes.value = __themes;
const __opts = themeOpts.value;
const __pool = fillThemePool.value;

// 旧分组结构回归：把同一批主题重新装回 group 项，喂给 fillThemePool
const __flat = __themes.map(function (t) { return { label: t.label, value: t.id }; });
const __by = function (p) {
  return __flat.filter(function (o) { return String(o.value).indexOf(p) === 0; });
};
const __rest = __flat.filter(function (o) {
  var v = String(o.value);
  return v.indexOf("scene_") !== 0 && v.indexOf("tpl_") !== 0
    && v.indexOf("art_") !== 0;
});
const __grouped = [];
if (__by("scene_").length) __grouped.push({ type: "group", label: "完整场景",
  key: "g_scene", children: __by("scene_") });
Array.prototype.push.apply(__grouped, __rest);
if (__by("tpl_").length) __grouped.push({ type: "group", label: "立绘模板",
  key: "g_tpl", children: __by("tpl_") });
if (__by("art_").length) __grouped.push({ type: "group", label: "画风",
  key: "g_art", children: __by("art_") });
themeOpts = { get value() { return __grouped; } };
const __poolGrouped = fillThemePool.value;

console.log(JSON.stringify({ opts: __opts, pool: __pool,
                             poolGrouped: __poolGrouped }));
"""

JS_HEAD = """const Vue = {
  ref: function (v) { return { value: v }; },
  computed: function (f) { return { get value() { return f(); } }; }
};
"""


def main():
    page = PAGE.read_text(encoding="utf-8")
    seg_opts = slice_between(page, "      var themes = Vue.ref([]);",
                             "      async function loadThemes()")
    seg_pool = slice_between(page, "      // 可轮换主题池",
                             "      async function startGalFill()")
    themes = [{"id": str(t["id"]), "label": str(t["label"])}
              for t in prompt_gen.THEMES]

    js = (JS_HEAD + seg_opts + seg_pool
          + JS_TAIL.replace("__THEME_JSON__",
                            json.dumps(themes, ensure_ascii=False)))
    tmp = ROOT / "_tmp_theme_options.js"
    tmp.write_text(js, encoding="utf-8")
    try:
        r = subprocess.run(["node", str(tmp)], capture_output=True, text=True,
                           encoding="utf-8")
    finally:
        tmp.unlink(missing_ok=True)
    if r.returncode != 0:
        print("FAIL: node 执行页面函数失败\n" + (r.stderr or "")[:3000])
        sys.exit(1)
    data = json.loads(r.stdout.strip().splitlines()[-1])
    opts, pool, pooled = data["opts"], data["pool"], data["poolGrouped"]

    all_ids = [t["id"] for t in themes]
    opt_ids = [str(o.get("value") or "") for o in opts]
    pool_ids = [str(o.get("id") or "") for o in pool]
    grouped_ids = [str(o.get("id") or "") for o in pooled]
    art_ids = [i for i in all_ids if i.startswith("art_")]
    backend_ids = list(prompt_gen.content_theme_ids())

    # ① 下拉平铺、不丢主题
    check("下拉不含 type:group 组项（vendored naive 不渲染表头）",
          not any(o.get("type") == "group" for o in opts))
    check("下拉每项都有 value 与 label",
          all(o.get("value") and o.get("label") for o in opts))
    check("下拉覆盖后端全部主题（不重不漏）",
          len(opt_ids) == len(all_ids) and sorted(opt_ids) == sorted(all_ids),
          f"opts={len(opt_ids)} themes={len(all_ids)}")
    for pfx in ("scene_", "tpl_", "art_"):
        check(f"下拉含全部 {pfx} 主题",
              sorted(i for i in opt_ids if i.startswith(pfx))
              == sorted(i for i in all_ids if i.startswith(pfx)))
    ranks = {"scene_": 0, "tpl_": 2, "art_": 3}

    def rank(v):
        for p, r in ranks.items():
            if v.startswith(p):
                return r
        return 1

    check("下拉分类顺序：完整场景 → 普通 → 模板 → 画风",
          all(rank(a) <= rank(b) for a, b in zip(opt_ids, opt_ids[1:])))

    # ② 轮换池：与后端同口径，画风一个不少
    check("轮换池与后端 content_theme_ids 完全同口径",
          sorted(pool_ids) == sorted(backend_ids),
          f"pool={len(pool_ids)} backend={len(backend_ids)}")
    check("轮换池排除 random / tpl_ / scene_",
          all(v != "random" and not v.startswith(("tpl_", "scene_"))
              for v in pool_ids))
    check("轮换池包含全部画风主题（② 回归）",
          all(i in pool_ids for i in art_ids),
          f"art={len(art_ids)} 命中={sum(i in pool_ids for i in art_ids)}")
    check("喂回分组结构时画风组也不整组漏掉（② 回归）",
          sorted(grouped_ids) == sorted(pool_ids),
          f"grouped={len(grouped_ids)} pool={len(pool_ids)}")

    if FAILURES:
        print(f"test_theme_options: {len(FAILURES)} FAILED")
        sys.exit(1)
    print("test_theme_options: ALL OK")


if __name__ == "__main__":
    main()
