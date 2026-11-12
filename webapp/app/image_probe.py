# -*- coding: utf-8 -*-
"""图片头信息探测 —— 纯标准库实现，不需要 cv2。

用途：在算法依赖不可用时，仍能对上传图片做**可运行**的格式与尺寸校验。
依据 traceguard_dct.py L65-68 / L113 / L176：块数 = (h//8)*(w//8)，需 >= 96。
注意：通道数与 dtype 校验必须等 cv2 解码后执行，本模块只做头部静态探测，不冒充解码。
"""
from __future__ import annotations

import struct

from . import config

PNG_SIG = b"\x89PNG\r\n\x1a\n"


def probe(data: bytes) -> dict:
    """返回 {format, width, height, n_blocks, too_small, note}。"""
    if not data:
        return {"format": None, "width": None, "height": None,
                "n_blocks": None, "too_small": None, "note": "空文件"}
    if data.startswith(PNG_SIG) and len(data) >= 24:
        if data[12:16] != b"IHDR":
            return _unknown("PNG 缺少 IHDR")
        w, h = struct.unpack(">II", data[16:24])
        bit_depth = data[24]
        color_type = data[25]
        ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
        return _sized("PNG", w, h,
                      f"bit_depth={bit_depth}, color_type={color_type}, 通道提示={ch}")
    if data.startswith(b"BM") and len(data) >= 26:
        w, h = struct.unpack("<ii", data[18:26])
        return _sized("BMP", abs(w), abs(h), "BMP 头，通道/dtype 需解码后确认")
    if data.startswith(b"\xff\xd8"):
        # JPEG SOF 扫描（标准库实现，仅取尺寸）
        i, wh = 2, None
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seglen = struct.unpack(">H", data[i + 2:i + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                wh = (w, h)
                break
            if seglen < 2:
                break
            i += 2 + seglen
        if wh:
            return _sized("JPEG", wh[0], wh[1],
                          "JPEG 有损格式：结果图禁用 JPEG 保存")
        return _unknown("JPEG 未找到 SOF 尺寸段")
    return _unknown("无法识别的图片格式（仅支持 PNG/BMP/JPEG 头探测）")


def _sized(fmt, w, h, note):
    n = (h // 8) * (w // 8)
    return {"format": fmt, "width": w, "height": h, "n_blocks": n,
            "too_small": n < config.MIN_BLOCKS,
            "note": note + f"；块数={n}，下限={config.MIN_BLOCKS}"}


def _unknown(note):
    return {"format": None, "width": None, "height": None,
            "n_blocks": None, "too_small": None, "note": note}
