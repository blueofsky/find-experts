#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成 find-experts 市场头像（512x512 PNG，无第三方水印）。

思路：4 倍超采样再缩到 512，所有边缘都是抗锯齿的。
构图：深墨蓝底 + 琥珀色放大镜环 + 镜片内三个人形，中间那个高亮。

用法：
  python build_avatar.py            # 输出 avatar-512.png
  python build_avatar.py --preview  # 同时输出 1024 预览图
"""

import os
import sys

from PIL import Image, ImageDraw

S = 4                 # 超采样倍数
C = 1024 * S          # 画布边长（最终 1024）
OUT_512 = 512

BG = (16, 22, 32, 255)          # 深墨蓝
LENS = (26, 38, 52, 255)        # 镜片底色，比背景略亮一点，读成"玻璃"
AMBER = (245, 166, 35, 255)     # 环与手柄
FIG_MID = (255, 198, 92, 255)   # 中间那个人：亮琥珀
FIG_SIDE = (232, 240, 248, 92)  # 两侧的人：冷白 36% 透明度

CX, CY = 478 * S, 462 * S       # 镜片圆心
R_OUT = 300 * S                 # 环外径
RING = 64 * S                   # 环宽
R_IN = R_OUT - RING             # 镜片内径


def person(draw, cx, head_y, head_r, shoulder_w, bottom, fill):
    """头 + 圆肩半圆身，整体一个剪影。"""
    draw.ellipse([cx - head_r, head_y - head_r, cx + head_r, head_y + head_r], fill=fill)
    neck = head_r * 0.42
    draw.rounded_rectangle(
        [cx - neck, head_y + head_r * 0.45, cx + neck, head_y + head_r * 1.15],
        radius=neck, fill=fill,
    )
    # 肩：上半是圆角矩形，做出溜肩感
    half = shoulder_w // 2
    draw.rounded_rectangle(
        [cx - half, head_y + head_r * 0.95, cx + half, bottom],
        radius=int(shoulder_w * 0.42), fill=fill,
    )


def build():
    img = Image.new("RGBA", (C, C), BG)

    # 镜片底
    lens = Image.new("RGBA", (C, C), (0, 0, 0, 0))
    ImageDraw.Draw(lens).ellipse(
        [CX - R_IN, CY - R_IN, CX + R_IN, CY + R_IN], fill=LENS)
    img = Image.alpha_composite(img, lens)

    # 三个人形：整体缩到镜片内完整的尺寸，四边都留底色。
    # 试过让底边被镜片截断，但三个人一起截会显得像柱子，而且中间那个琥珀色
    # 会跟琥珀色环贴在一起糊成一团——不如都完整，读起来是「三个候选人」。
    figs = Image.new("RGBA", (C, C), (0, 0, 0, 0))
    fd = ImageDraw.Draw(figs)
    head_y = CY - 116 * S
    head_r = 34 * S
    body_bottom = CY + 150 * S
    for cx, fill, w in (
        (CX - 124 * S, FIG_SIDE, 100 * S),
        (CX + 124 * S, FIG_SIDE, 100 * S),
        (CX, FIG_MID, 112 * S),
    ):
        person(fd, cx, head_y, head_r, w, body_bottom, fill)

    # 裁掉可能触到镜片边缘的部分，兜底防止算错尺寸时溢出到环上
    mask = Image.new("L", (C, C), 0)
    ImageDraw.Draw(mask).ellipse([CX - R_IN, CY - R_IN, CX + R_IN, CY + R_IN], fill=255)
    figs.putalpha(Image.composite(figs.getchannel("A"), Image.new("L", (C, C), 0), mask))
    img = Image.alpha_composite(img, figs)

    # 环 + 手柄
    d = ImageDraw.Draw(img)
    d.ellipse([CX - R_OUT, CY - R_OUT, CX + R_OUT, CY + R_OUT],
              outline=AMBER, width=RING)

    # 手柄：45 度方向，圆头收尾
    hx0, hy0 = CX + R_OUT * 0.62, CY + R_OUT * 0.62
    hx1, hy1 = CX + (R_OUT + 150 * S) * 0.72, CY + (R_OUT + 150 * S) * 0.72
    d.line([hx0, hy0, hx1, hy1], fill=AMBER, width=int(RING * 1.05))
    cap = int(RING * 1.05) // 2
    d.ellipse([hx1 - cap, hy1 - cap, hx1 + cap, hy1 + cap], fill=AMBER)

    # 背景是满幅的，导出时不做圆角裁切；市场端自己会裁
    out512 = img.convert("RGB").resize((OUT_512, OUT_512), Image.LANCZOS)
    return out512, img.convert("RGB")


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    out512, big = build()

    p512 = os.path.join(here, "avatar-512.png")
    out512.save(p512, "PNG", optimize=True)
    print("%s  %dx%d  %.1f KB" % (p512, *out512.size, os.path.getsize(p512) / 1024))

    if "--preview" in sys.argv:
        pprev = os.path.join(here, "avatar-preview-1024.png")
        big.resize((1024, 1024), Image.LANCZOS).save(pprev, "PNG", optimize=True)
        print("%s  %dx%d" % (pprev, 1024, 1024))

    # 64px 缩略图，用来自查小尺寸可辨识度
    p64 = os.path.join(here, "avatar-64-check.png")
    out512.resize((64, 64), Image.LANCZOS).save(p64, "PNG")
    print("%s  64x64（自查用）" % p64)


if __name__ == "__main__":
    main()
