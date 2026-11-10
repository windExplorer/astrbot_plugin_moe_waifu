# -*- coding: utf-8 -*-
"""draw.py 纯函数自检（可直接运行，退出码即结论；不联网、不依赖萌绘）。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import draw  # noqa: E402


def check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    print(f"ok: {name}")


# parse_draw_response
check("JSON image_paths", draw.parse_draw_response(
    '{"image_paths": ["/a/b.png"], "status": "ok"}') == ["/a/b.png"])
check("旧单数 image_path", draw.parse_draw_response('{"image_path": "/x.png"}') == ["/x.png"])
check("花括号截取", draw.parse_draw_response('前缀噪音 {"image_paths": ["/p.png"]} 后缀') == ["/p.png"])
check("坏文本返回空", draw.parse_draw_response("完全不是 JSON") == [])
check("空输入返回空", draw.parse_draw_response(None) == [])

# build_prompt
entry = {"tags_en": "hatsune miku, twintails"}
p = draw.build_prompt(entry)
check("提示词含角色 tags", "hatsune miku" in p and "twintails" in p)
check("提示词含单人构图词", p.startswith("1girl, solo"))
check("空 tags 也成立", draw.build_prompt({}).startswith("1girl"))

# resolve_negative
cfg = {"draw_negative_enabled": True, "draw_negative_prompt": ""}
neg = draw.resolve_negative(cfg)
check("默认兜底含排除男性", "1boy" in neg and "nsfw" in neg)
check("自定义覆盖", draw.resolve_negative(
    {"draw_negative_enabled": True, "draw_negative_prompt": "custom"}) == "custom")
check("关闭则不传", draw.resolve_negative({"draw_negative_enabled": False}) == "")

# decision_of
check("当日已有→复用", draw.decision_of("a", "d", {"file": "x"}, 3, 5) == "reuse_today")
check("未满配额→生成", draw.decision_of("a", "d", None, 3, 5) == "generate")
check("满配额→取库", draw.decision_of("a", "d", None, 5, 5) == "from_pool")

# archive_portrait：拷贝进库并返回相对路径
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    src = td / "raw.png"
    src.write_bytes(b"PNGDATA")
    rel = draw.archive_portrait(str(src), td, "miku", "2026-11-11", 0, 12345)
    dest = td / rel
    check("归档文件存在且内容一致", dest.is_file() and dest.read_bytes() == b"PNGDATA")
    check("归档路径形状", rel == "portraits/miku/2026-11-11_000_12345.png")

# parse_workflow_list（v0.3.4：默认工作流小节必须排除）
ANIMA_RAW = """已配置的工作流列表：
- 动漫-通用 [支持图生图] 【Anima】 [底模 NoobAI]
  · 描述：主力动漫工作流（这行说明里也提到图生图三个字，不能被误当条目）
- 动漫-文生图专用 [仅文生图] 【Anima】
- 真人-通用 [支持图生图]
默认工作流：
- 动漫文生图: 动漫-通用
- 真人图生图: 真人-通用
"""
wf = draw.parse_workflow_list(ANIMA_RAW)
check("解析出 3 条工作流", len(wf) == 3)
check("默认工作流小节被排除", all("动漫文生图:" not in w["name"] for w in wf))
check("图生图支持标记", [w["name"] for w in wf if w["img2img"]] == ["动漫-通用", "真人-通用"])
check("Anima 标记", [w["name"] for w in wf if w["anima"]] == ["动漫-通用", "动漫-文生图专用"])
check("说明行不误入", all("描述" not in w["name"] for w in wf))
check("空输入", draw.parse_workflow_list("") == [])

print("test_draw: ALL OK")
