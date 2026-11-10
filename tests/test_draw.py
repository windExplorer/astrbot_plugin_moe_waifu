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

# workflow_img2img_capable（v0.3.6 用户口径：绑定基础工作流以解析注记为准，
# 未绑定看名字，手填 image_node 亦可用）
check("新版绑定+解析有图节点", draw.workflow_img2img_capable(
    {"base_id": "12"}, {"image_node": "12"}) is True)
check("新版绑定+解析无图节点", draw.workflow_img2img_capable(
    {"base_id": "12"}, {"image_node": ""}) is False)
check("新版绑定+注记查询失败兜底可用", draw.workflow_img2img_capable(
    {"base_id": "12"}, None) is True)
check("未绑定+名字带图生图", draw.workflow_img2img_capable(
    {"name": "动漫图生图专用"}) is True)
check("未绑定+手填 image_node", draw.workflow_img2img_capable(
    {"name": "moe", "image_node": "12"}) is True)
check("未绑定纯文生图", draw.workflow_img2img_capable(
    {"name": "动漫文生图"}) is False)

# effective_entry（v0.4.0 试画台临时参数 → 生效条目）
base_entry = {"id": "miku", "name": "初音未来", "strategy": {"type": "base"},
              "strategy_type": "base", "tags_en": "hatsune miku"}
eff = draw.effective_entry(base_entry, {"strategy_type": "img2img", "ref": "refs/a.png",
                                        "denoise": 0.6})
check("覆盖为 img2img", eff["strategy_type"] == "img2img"
      and eff["strategy"]["type"] == "img2img")
check("参考图/重绘强度写入", eff["strategy"]["ref"] == "refs/a.png"
      and eff["strategy"]["denoise"] == 0.6)
check("原条目不被修改", base_entry["strategy"]["type"] == "base")
eff2 = draw.effective_entry(base_entry, {"strategy_type": "img2img", "ref": "x.png"})
check("未传 denoise 不写入", "denoise" not in eff2["strategy"])
eff3 = draw.effective_entry(base_entry, None)
check("无覆盖=原样", eff3["strategy"]["type"] == "base")
eff4 = draw.effective_entry(base_entry, {"strategy_type": "lora", "loras": ["m:0.8", " "]})
check("lora 覆盖", eff4["strategy"]["loras"] == ["m:0.8"])

print("test_draw: ALL OK")
