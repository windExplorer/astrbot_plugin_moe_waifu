# -*- coding: utf-8 -*-
"""头像方形直出（v0.59.0）纯函数守卫，可直接运行，退出码即结论。

盯住：配置读取与对齐 / 方形产物不裁剪只缩放 / 非方形兜底裁方 / 契约接线。
"""
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILURES = []


def check(name, cond, detail=""):
    if not cond:
        FAILURES.append(name)
        print(f"FAIL: {name} {detail}")
    else:
        print(f"ok: {name}")


def main():
    import avatars

    # 1) 配置读取与对齐
    check("默认边长 1024（无配置）", avatars.avatar_gen_size({}) == 1024)
    check("配置值生效且对齐 16", avatars.avatar_gen_size({"avatar_size": 800}) == 800)
    check("非 16 步长对齐", avatars.avatar_gen_size({"avatar_size": 1000}) == 992)
    check("过小回落默认", avatars.avatar_gen_size({"avatar_size": 10}) == 1024)
    check("过大回落默认", avatars.avatar_gen_size({"avatar_size": 99999}) == 1024)
    check("脏值不炸", avatars.avatar_gen_size({"avatar_size": "abc"}) == 1024)

    try:
        from PIL import Image
    except ImportError:
        print("test_avatar_square: SKIP (no Pillow)")
        return

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        # 2) 方形产物：不裁剪，只缩放（用一个「脸」在正中 1/4 的图，裁方会把它挪位）
        sq = Image.new("RGB", (1200, 1200), (20, 20, 20))
        for x in range(600, 900):
            for y in range(150, 450):
                sq.putpixel((x, y), (255, 0, 0))
        sq_src = td / "square.png"
        sq.save(sq_src)
        out = td / "out_square.png"
        w, h = avatars.finalize_avatar(sq_src, out, 1024)
        check("方形产物归一到目标边长", (w, h) == (1024, 1024))
        with Image.open(out) as im:
            check("方形产物尺寸落盘正确", im.size == (1024, 1024))
            # 不裁剪：原图正中偏上色块 (600~900,150~450) 缩放后应仍在
            # 约 (512~768,128~384) 区间；取中心点 (640,256) 应为红
            check("方形产物未被二次裁切（构图保留）",
                  im.getpixel((640, 256))[:3] == (255, 0, 0))

        # 3) 非方形产物（工作流忽略尺寸）：回退偏上裁方
        tall = Image.new("RGB", (700, 1400), (0, 255, 0))
        for x in range(250, 450):
            for y in range(120, 320):
                tall.putpixel((x, y), (0, 0, 255))
        tall_src = td / "tall.png"
        tall.save(tall_src)
        out2 = td / "out_tall.png"
        w2, h2 = avatars.finalize_avatar(tall_src, out2, 512)
        check("非方形兜底裁方并归一", (w2, h2) == (512, 512))

        # 4) crop_square 仍是 512（手动上传/裁取路径不变）
        out3 = td / "crop.png"
        w3, h3 = avatars.crop_square(tall_src, out3)
        check("crop_square 手动路径仍 512", (w3, h3) == (512, 512))

    # 5) 契约接线：配置键在 schema / _draw_cfg / 前端分区 / 生成链路
    schema = (ROOT / "_conf_schema.json").read_text(encoding="utf-8")
    main_py = (ROOT / "main.py").read_text(encoding="utf-8")
    api = (ROOT / "webui_api.py").read_text(encoding="utf-8")
    page = (ROOT / "pages" / "waifu-console" / "index.html").read_text(encoding="utf-8")
    check("schema 有 avatar_size 默认 1024",
          '"avatar_size"' in schema and '"default": 1024' in schema)
    check("_draw_cfg 读 avatar_size", '"avatar_size": self._cfg("avatar_size", 1024)' in main_py)
    check("前端配置分区登记 avatar_size", '"avatar_size"' in page)
    check("generate_avatar_for 用方形尺寸 + finalize_avatar",
          "avatar_gen_size(cfg)" in api
          and 'cfg["draw_width"] = avatar_side' in api
          and "finalize_avatar(src, dst, avatar_side)" in api)

    print("test_avatar_square: " + ("ALL OK" if not FAILURES else f"{len(FAILURES)} FAILED"))
    sys.exit(0 if not FAILURES else 1)


if __name__ == "__main__":
    main()
