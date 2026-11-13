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
# v0.19.3：img2img 模式不受满配额短路（用户实报：配了只用图生图模板却一直取立绘）
check("img2img 满配额→仍走生成决策",
      draw.decision_of("a", "d", None, 5, 5, "img2img") == "generate")
# v0.19.6：img2img 连「当日已有立绘」也不复用（每次都按出卡源现画）
check("img2img 当日已有立绘→仍现画（不 reuse_today）",
      draw.decision_of("a", "d", {"file": "x"}, 5, 5, "img2img") == "generate")
check("mix 当日已有立绘→复用（同角色同天共享不变）",
      draw.decision_of("a", "d", {"file": "x"}, 5, 5, "mix") == "reuse_today")
check("mix 满配额→取库（mode 参与判定后行为不变）",
      draw.decision_of("a", "d", None, 5, 5, "mix") == "from_pool")
check("pool 满配额→取库", draw.decision_of("a", "d", None, 9, 5, "pool") == "from_pool")
check("未知 mode 按老规则（满配额取库）",
      draw.decision_of("a", "d", None, 5, 5, "what") == "from_pool")

# 图生图源判定（v0.10.0：与「参考图列表」彻底解耦）
# 用户口径：没显式指定图生图参考图，就不能私自拿参考图列表里的图去生成老婆。
import asyncio  # noqa: E402

with tempfile.TemporaryDirectory() as ref_td:
    ref_dir = Path(ref_td)
    (ref_dir / "a.png").write_bytes(b"PNG")
    (ref_dir / "refs").mkdir()
    (ref_dir / "refs" / "b.png").write_bytes(b"PNG")
    b_abs = str((ref_dir / "refs" / "b.png").resolve())

    # has_template_ref：试画/补图守卫，只认显式 strategy.ref
    check("显式主参考可用", draw.has_template_ref(
        {"strategy": {"ref": str(ref_dir / "a.png")}}) is True)
    check("只有参考图列表不算显式指定", draw.has_template_ref(
        {"strategy": {"refs": ["", "refs/b.png"]}}, ref_dir) is False)
    check("URL 主参考可用", draw.has_template_ref(
        {"strategy": {"ref": "https://example.com/a.png"}}) is True)
    check("主参考文件失效不算", draw.has_template_ref(
        {"strategy": {"ref": "a.png"}}) is False)
    check("无 strategy 不算", draw.has_template_ref({}) is False)
    check("pick_ref 相对路径解析（仅资料库用）",
          draw.pick_ref(["refs/b.png"], 0, ref_dir) == b_abs)
    check("pick_ref 失效路径跳过", draw.pick_ref(["nope.png"], 0, ref_dir) == "")

    # 出卡源 strategy.draw_ref：独立于 ref / refs
    check("出卡源相对路径解析", draw.draw_source(
        {"strategy": {"draw_ref": "refs/b.png"}}, ref_dir) == b_abs)
    check("有主参考/参考图但没指定出卡源→不规划图生图", draw.has_draw_source(
        {"strategy": {"ref": str(ref_dir / "a.png"),
                      "refs": ["refs/b.png"]}}, ref_dir) is False)
    check("指定了出卡源→可用", draw.has_draw_source(
        {"strategy": {"draw_ref": "refs/b.png"}}, ref_dir) is True)
    check("失效出卡源不算", draw.has_draw_source(
        {"strategy": {"draw_ref": "refs/gone.png"}}, ref_dir) is False)

    seen = {}

    class _FakeTool:
        def __init__(self, name):
            self.name = name
        async def handler(self, event, **kwargs):
            seen.clear()
            seen.update(kwargs)
            seen["tool"] = self.name
            return '{"image_paths": []}'

    class _FakeManager:
        def get_func(self, name):
            return _FakeTool(name)

    class _FakeContext:
        def get_llm_tool_manager(self):
            return _FakeManager()

    cfg_ref = {"data_dir": ref_dir}
    # 只有参考图列表、没有显式源 → 回退文生图，绝不私自取列表里的图
    only_refs = {"strategy_type": "img2img", "tags_en": "miku",
                 "strategy": {"refs": ["refs/b.png"]}}
    r1 = asyncio.run(draw.generate_portrait(_FakeContext(), cfg_ref, only_refs, 1))
    check("没指定源时返回 4 元组", isinstance(r1, tuple) and len(r1) == 4)
    check("没指定源回退文生图（comfyui_draw）", seen.get("tool") == "comfyui_draw")
    check("没指定源不传 image", "image" not in seen)
    # v0.13.4：文生图链也传「尺寸开关」——绑定了尺寸来源开关的新版工作流上，
    # 只传宽高而 custom_size=None 会被工作流配置（默认关）忽略，配置宽高不生效
    check("文生图传配置宽高 + 尺寸开关",
          seen.get("width") == 704 and seen.get("height") == 1200
          and seen.get("custom_size") is True)
    # 显式指定 → 只用这一张
    explicit = {"strategy_type": "img2img", "tags_en": "miku",
                "strategy": {"ref": "refs/b.png", "refs": ["refs/a.png"]}}
    r2 = asyncio.run(draw.generate_portrait(_FakeContext(), cfg_ref, explicit, 1))
    check("显式指定也返回 4 元组", len(r2) == 4)
    check("显式指定用 img2img 工具", seen.get("tool") == "comfyui_img2img")
    # v0.12.6：传的是 refs_stage/ 副本（萌绘归档会 os.replace 搬走源文件）
    check("显式指定的图以 staging 副本交给萌绘",
          "refs_stage" in str(seen.get("image") or ""))
    check("原参考图未被搬走（仍在 refs/）", (ref_dir / "refs" / "b.png").is_file())

    # ---- v0.13.3：img2img 尺寸——默认配置宽高 + custom_size，显式覆盖优先 ----
    # 旧版只有多视图底稿才传宽高，普通重绘输出尺寸跟参考图走（多视图源转单图
    # 得横版单图）。现在默认传配置 draw_width/height（对齐 16：700→704）。
    check("img2img 默认传配置宽高 + 尺寸开关",
          seen.get("width") == 704 and seen.get("height") == 1200
          and seen.get("custom_size") is True)
    # 多视图底稿提示词 → 自动横/方版（v0.7.3 行为保持）
    mv_entry = {"strategy_type": "img2img", "tags_en": "miku",
                "strategy": {"ref": "refs/b.png"}}
    asyncio.run(draw.generate_portrait(
        _FakeContext(), cfg_ref, mv_entry, 1,
        prompt="三视图（正面、侧面、背面同框，灰色纯色背景），保持角色外观不变"))
    check("多视图底稿提示词用横版尺寸",
          seen.get("width") == 1216 and seen.get("height") == 832
          and seen.get("custom_size") is True)
    # 工坊 img2img 面板的显式尺寸（strategy.width/height）优先于一切
    sized = {"strategy_type": "img2img", "tags_en": "miku",
             "strategy": {"ref": "refs/b.png", "width": 512, "height": 768}}
    asyncio.run(draw.generate_portrait(_FakeContext(), cfg_ref, sized, 1))
    check("显式尺寸覆盖默认", seen.get("width") == 512 and seen.get("height") == 768)
    # 只填一边 → 视同未指定，回配置默认
    half = {"strategy_type": "img2img", "tags_en": "miku",
            "strategy": {"ref": "refs/b.png", "width": 512}}
    asyncio.run(draw.generate_portrait(_FakeContext(), cfg_ref, half, 1))
    check("只填宽不生效（回配置默认）",
          seen.get("width") == 704 and seen.get("height") == 1200)
    check("_strategy_size 成对生效",
          draw._strategy_size({"width": 512, "height": 768}) == (512, 768))
    check("_strategy_size 单边/越界/非 dict 不生效",
          draw._strategy_size({"width": 512}) == (0, 0)
          and draw._strategy_size({"width": 9999, "height": 768}) == (0, 0)
          and draw._strategy_size(None) == (0, 0))
    # effective_entry：本次显式尺寸写进 strategy（成对生效，单边弹旧值防残留）
    plain_entry = {"id": "miku", "name": "初音未来", "strategy": {"type": "base"},
                   "strategy_type": "base", "tags_en": "miku"}
    eff_w = draw.effective_entry(plain_entry, {"strategy_type": "img2img",
                                               "ref": "x.png",
                                               "width": 512, "height": 768})
    check("override 尺寸写进 strategy",
          eff_w["strategy"].get("width") == 512
          and eff_w["strategy"].get("height") == 768)
    eff_h = draw.effective_entry(plain_entry, {"strategy_type": "img2img",
                                               "ref": "x.png", "width": 512})
    check("override 单边尺寸不残留",
          "width" not in eff_h["strategy"] and "height" not in eff_h["strategy"])

# plan_source（v0.9.6 出图方式规划：mix/pool/img2img 三模式 + 回退链）
# v0.19.3：满配额短路只对 mix / pool 生效；img2img 语义是「每次都按出卡源现画」
check("img2img 满配额仍现画（v0.19.3 修「配了只用图生图模板却一直取立绘」）",
      draw.plan_source("img2img", 5, 5, True, 0, 100) == "generate_i2i")
check("img2img 满配额且没出卡源→回退取库",
      draw.plan_source("img2img", 5, 5, False, 0, 100) == "from_pool")
check("mix 满配额→取库（不浪费算力）",
      draw.plan_source("mix", 5, 5, True, 0, 100) == "from_pool")
check("pool 满配额→取库", draw.plan_source("pool", 6, 5, True, 0, 100) == "from_pool")
# mix
check("mix 命中 i2i 概率", draw.plan_source("mix", 2, 5, True, 30, 50) == "generate_i2i")
check("mix 未中→取库", draw.plan_source("mix", 2, 5, True, 80, 50) == "from_pool")
check("mix 命中但没模板图→取库", draw.plan_source("mix", 2, 5, False, 30, 50) == "from_pool")
check("mix 库空+有模板图→i2i", draw.plan_source("mix", 0, 5, True, 99, 0) == "generate_i2i")
check("mix 库空+无模板图→老链路", draw.plan_source("mix", 0, 5, False, 30, 50) == "generate")
# pool：只用库存
check("pool 有库存→取库", draw.plan_source("pool", 1, 5, True, 0, 100) == "from_pool")
check("pool 库空→老链路", draw.plan_source("pool", 0, 5, True, 0, 100) == "generate")
# img2img：只用模板图生图
check("img2img 有模板图→i2i", draw.plan_source("img2img", 2, 5, True, 99, 0) == "generate_i2i")
check("img2img 无模板图→回退取库", draw.plan_source("img2img", 2, 5, False, 0, 0) == "from_pool")
check("img2img 无模板图且库空→老链路",
      draw.plan_source("img2img", 0, 5, False, 0, 0) == "generate")
# 非法 mode 当 mix
check("非法 mode→mix", draw.plan_source("what", 2, 5, True, 0, 100) == "generate_i2i")

# ---------- explain_decision（v0.19.4：这次为什么这么出图，落库+日志+工坊同一句） ----------
check("说明-当日复用（同角色同天共享，并给出重画办法）",
      "同角色同一天人人同款" in draw.explain_decision("mix", "reuse_today", 3, 5, True)
      and "重试今日老婆立绘" in draw.explain_decision("mix", "reuse_today", 3, 5, True))
_az_shared = draw.explain_decision("img2img", "generate_i2i", 2, 5, True, 50, 10,
                                   today_portrait=True)
check("说明-img2img 当日已有立绘仍现画", "不共享、仍现画" in _az_shared)
_az = draw.explain_decision("img2img", "generate_i2i", 5, 5, True)
check("说明-img2img 满配额仍现画", "只用图生图模板" in _az and "仍现画" in _az)
_az2 = draw.explain_decision("img2img", "from_pool", 5, 5, False)
check("说明-img2img 无出卡源→回退取库",
      "未指定「出卡图生图源」" in _az2 and "回退" in _az2)
check("说明-pool 模式", "只用立绘库" in draw.explain_decision("pool", "from_pool", 2, 5, True))
_full = draw.explain_decision("mix", "from_pool", 5, 5, True, 50, 10)
check("说明-mix 满配额取库(不浪费算力)", "已满配额" in _full)
_miss = draw.explain_decision("mix", "from_pool", 2, 5, True, 50, 80)
check("说明-mix 掷点未命中", "掷点 80" in _miss and "≥" in _miss)
_noref = draw.explain_decision("mix", "from_pool", 2, 5, False, 50, 10)
check("说明-mix 命中但没出卡源→回退", "未指定" in _noref)
_hit = draw.explain_decision("mix", "generate_i2i", 2, 5, True, 50, 10)
check("说明-mix 掷点命中", "掷点 10" in _hit and "<" in _hit)
check("说明-库存空老链路", "文生图" in draw.explain_decision("mix", "generate", 0, 5, False))
check("说明-脏输入不抛异常", isinstance(draw.explain_decision("", "", 0, 0, False), str))
# 概率钳制
check("i2i_prob=100 必中", draw.plan_source("mix", 2, 5, True, 99, 100) == "generate_i2i")
check("i2i_prob=0 不中", draw.plan_source("mix", 2, 5, True, 0, 0) == "from_pool")

# archive_portrait：拷贝进库并返回相对路径
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    src = td / "raw.png"
    src.write_bytes(b"PNGDATA")
    rel = draw.archive_portrait(str(src), td, "miku", "2026-11-11", 0, 12345)
    dest = td / rel
    check("归档文件存在且内容一致", dest.is_file() and dest.read_bytes() == b"PNGDATA")
    check("归档路径形状", rel == "portraits/miku/2026-11-11_000_12345.png")

    # v0.9.10：同日同角色同 seq 会算出同名，绝不覆盖旧图
    src2 = td / "raw2.png"
    src2.write_bytes(b"ANOTHER_PNG")
    rel2 = draw.archive_portrait(str(src2), td, "miku", "2026-11-11", 0, 12345)
    check("同名归档不覆盖旧立绘",
          rel2 != rel and (td / rel).read_bytes() == b"PNGDATA"
          and (td / rel2).read_bytes() == b"ANOTHER_PNG")
    rel3 = draw.archive_portrait(str(src2), td, "miku", "2026-11-11", 0, 12345)
    check("同内容归档复用不堆重复文件", rel3 == rel2)

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
# 条目级工作流覆盖（v0.7.2 补一张工作流下拉）
eff5 = draw.effective_entry(base_entry, {"workflow": "txt-anime"})
check("工作流覆盖写入 base", eff5["strategy"]["workflow"] == "txt-anime"
      and eff5["strategy_type"] == "base")
img_entry = {"id": "rem", "name": "蕾姆", "strategy": {"type": "img2img", "refs": ["r.png"]},
             "strategy_type": "img2img", "tags_en": "rem"}
eff6 = draw.effective_entry(img_entry, {"img2img_workflow": "img-clean"})
check("img2img 条目工作流覆盖走 img2img_workflow",
      eff6["strategy"]["img2img_workflow"] == "img-clean"
      and "workflow" not in eff6["strategy"])

# _first_sentence（v0.5.3：萌绘失败文本取首句，不吐提示语尾巴）
check("失败文本取首句", draw._first_sentence(
    "本次生图失败。请用一句话简短向用户说明生成遇到问题即可，不要复述本提示。") == "本次生图失败")
check("空文本兜底", draw._first_sentence("") == "萌绘未返回结果")
check("无句号截断限长", len(draw._first_sentence("x" * 200)) <= 80)

# detect_view_layout（v0.7.3：多视图底稿识别，中英都认）
check("英文三视图", draw.detect_view_layout(
    "character sheet, three views, front view, side view, back view") == "3view")
check("英文四视图", draw.detect_view_layout(
    "character sheet, multiple views, four views, simple background") == "4view")
check("中文三视图", draw.detect_view_layout("改成三视图（正面、侧面、背面同框，纯色背景）") == "3view")
check("中文四视图", draw.detect_view_layout("画一张四视图，服装改成泳装") == "4view")
check("普通立绘不误判", draw.detect_view_layout(
    "1girl, solo, standing, full body, front-facing, simple background") == "")
check("空输入", draw.detect_view_layout("") == "")

# 多视图尺寸 + 负面剥离（v0.7.3）
cfg_mv = {"draw_negative_enabled": True, "draw_negative_prompt": "", "draw_width": 700,
          "draw_height": 1200}
p3 = draw.build_prompt({"tags_en": "hatsune miku, character sheet, three views, "
                                     "front view, side view, back view, simple background"})
kw3 = draw._base_kwargs(cfg_mv, p3, 1)
check("三视图→横版 1216×832", kw3["width"] == 1216 and kw3["height"] == 832)
check("三视图负面剥 multiple views/girls",
      "multiple views" not in kw3["negative_prompt"]
      and "multiple girls" not in kw3["negative_prompt"]
      and "1boy" in kw3["negative_prompt"])
p4 = draw.build_prompt({"tags_en": "rem, character sheet, multiple views, four views"})
kw4 = draw._base_kwargs(cfg_mv, p4, 1)
check("四视图→方形 1024×1024", kw4["width"] == 1024 and kw4["height"] == 1024)
check("四视图负面同样剥离", "multiple views" not in kw4["negative_prompt"])
kw_norm = draw._base_kwargs(cfg_mv, "1girl, solo, standing", 1)
check("普通立绘→配置尺寸并对齐16（700→704）",
      kw_norm["width"] == 704 and kw_norm["height"] == 1200)
kw_none = draw._base_kwargs({"draw_negative_enabled": False}, "普通", 1)
check("负面关闭→不传", "negative_prompt" not in kw_none)
check("resolve_negative 兼容旧签名", draw.resolve_negative(cfg_mv).startswith("1boy"))
check("_snap16", draw._snap16(700) == 704 and draw._snap16(1200) == 1200
      and draw._snap16("bad") == 1024)

# v0.11.0：多图参考（gen_refs / refs override / images 透传 + 旧版萌绘降级）
with tempfile.TemporaryDirectory() as multi_td:
    mdir = Path(multi_td)
    for n in ("front.png", "side.png", "back.png"):
        (mdir / n).write_bytes(b"PNG")
    st_multi = {"refs": ["refs/library-only.png"],
                "gen_refs": ["front.png", "side.png"]}
    front_abs = str((mdir / "front.png").resolve())
    side_abs = str((mdir / "side.png").resolve())
    check("gen_refs 按顺序解析", draw.gen_refs(st_multi, mdir)
          == [front_abs, side_abs])
    check("资料库 refs 不算本次多图源", draw.gen_refs(
        {"refs": ["front.png"]}, mdir) == [])
    check("has_gen_refs 认多图源",
          draw.has_gen_refs({"strategy": st_multi}, mdir) is True)
    check("has_gen_refs 不认资料库", draw.has_gen_refs(
        {"strategy": {"refs": ["front.png"]}}, mdir) is False)

    eff = draw.effective_entry(
        {"strategy": {"refs": ["refs/library.png"]}, "strategy_type": "base"},
        {"strategy_type": "img2img", "refs": ["front.png", "side.png"]})
    check("override refs 写入 gen_refs",
          eff["strategy"]["gen_refs"] == ["front.png", "side.png"])
    check("override refs 不碰资料库 refs",
          eff["strategy"]["refs"] == ["refs/library.png"])
    check("override refs 首张回填 ref", eff["strategy"]["ref"] == "front.png")

    seen2 = {}

    class _MultiTool:
        async def handler(self, event, **kwargs):
            seen2.clear()
            seen2.update(kwargs)
            return '{"image_paths": []}'

    class _MultiManager:
        def get_func(self, name):
            return _MultiTool()

    class _MultiCtx:
        def get_llm_tool_manager(self):
            return _MultiManager()

    multi_entry = {"strategy_type": "img2img", "tags_en": "miku",
                   "strategy": {"gen_refs": ["front.png", "side.png", "back.png"]}}
    r_multi = asyncio.run(draw.generate_portrait(
        _MultiCtx(), {"data_dir": mdir}, multi_entry, 1))
    check("多图源返回 4 元组", isinstance(r_multi, tuple) and len(r_multi) == 4)
    check("多图源传 images（3 张）", len(seen2.get("images") or []) == 3)
    check("image 是第一张的 staging 副本",
          "refs_stage" in str(seen2.get("image") or "")
          and Path(seen2["image"]).name == "00.png")
    check("多图参考原文件都还在",
          all((mdir / n).is_file() for n in ("front.png", "side.png", "back.png")))

    class _OldTool:
        def __init__(self):
            self.calls = []
        async def handler(self, event, **kwargs):
            self.calls.append(dict(kwargs))
            if "images" in kwargs:
                raise TypeError("unexpected keyword argument 'images'")
            return '{"image_paths": []}'

    old_tool = _OldTool()

    class _OldManager:
        def get_func(self, name):
            return old_tool

    class _OldCtx:
        def get_llm_tool_manager(self):
            return _OldManager()

    r_old = asyncio.run(draw.generate_portrait(
        _OldCtx(), {"data_dir": mdir}, multi_entry, 1))
    check("旧版萌绘不认 images 时返回 4 元组", len(r_old) == 4)
    check("旧版萌绘不认 images 时降级单图重试",
          len(old_tool.calls) == 2 and "images" not in old_tool.calls[1])

print("test_draw: ALL OK")
