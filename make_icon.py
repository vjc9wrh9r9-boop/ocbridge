#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成程序图标 OCBridge.ico（需要 Pillow）。用法: python make_icon.py"""
from PIL import Image, ImageDraw, ImageFont


def make_icon_image(size=256):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = max(2, size // 32)
    d.ellipse((m, m, size - m, size - m), fill=(29, 101, 237, 255))
    try:
        font = ImageFont.truetype("arialbd.ttf", int(size * 0.42))
    except Exception:
        try:
            font = ImageFont.truetype("arial.ttf", int(size * 0.42))
        except Exception:
            font = ImageFont.load_default()
    d.text((size / 2, size / 2 + size * 0.02), "OC", fill="white", anchor="mm", font=font)
    return img


if __name__ == "__main__":
    img = make_icon_image(256)
    img.save("OCBridge.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("OCBridge.ico generated")
