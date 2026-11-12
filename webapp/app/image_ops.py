# -*- coding: utf-8 -*-
"""图像处理与指标 —— 仅服务于 A 组兼容接口（/api/*）。

职责边界
--------
本模块只做「上传字节 → 可交给算法的 BGR uint8 图」这一段，以及画质指标：
  * Base64 解码与体积上限
  * 文件完整性嗅探（JPEG FFD9 / PNG IEND）
  * 解码、位深与通道规范化（16bit/float、灰度、RGBA 白底合成）
  * 尺寸/块数/最短边校验，meta 汇总，中文 warnings
  * SSIM（与 A 组原实现逐行一致，保证数值可比）
  * JPEG 往返（真实有损重编码）

不做
----
  * 不实现、不复制任何水印算法逻辑 —— 算法调用统一走 core_adapter
  * 不读写项目历史结果文件（results.json / phase2_* / phase3_*）
  * 不落盘原始图片
"""
from __future__ import annotations

import base64

import cv2
import numpy as np
from scipy.ndimage import gaussian_filter

from . import config


class CompatError(Exception):
    """可以直接展示给用户看的错误（message 人话 + hint 怎么解决）。"""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.message = message
        self.hint = hint


# ------------------------------------------------------------------ Base64 输入

def b64_to_bytes(s) -> bytes:
    """把 dataURL 或纯 Base64 字符串还原成字节，并做体积校验。"""
    if not isinstance(s, str) or not s.strip():
        raise CompatError("没有收到图片数据", "请重新选择图片后再试")
    if s.startswith("data:"):
        s = s.split(",", 1)[-1]
    try:
        data = base64.b64decode(s, validate=False)
    except Exception:
        raise CompatError("图片数据格式不正确", "请重新上传一次")
    if len(data) > config.COMPAT_MAX_UPLOAD:
        raise CompatError(
            f"图片文件太大（超过 {config.COMPAT_MAX_UPLOAD // (1024 * 1024)} MB）",
            "请先压缩后再上传",
        )
    return data


def check_integrity(data: bytes) -> None:
    """文件尾嗅探。

    OpenCV 对截断的 JPEG 很宽容，会照常解码出一张带灰块的图。若直接嵌入，
    用户拿到的就是「损坏图 + 水印成功」的假结果，属于最危险的一类假成功。
    """
    if len(data) < 12:
        raise CompatError("文件太小，不可能是完整图片", "请重新导出或另存后再上传")
    if data[:2] == b"\xff\xd8":                       # JPEG: SOI ... EOI(FFD9)
        if data.rstrip(b"\x00")[-2:] != b"\xff\xd9":
            raise CompatError(
                "图片文件不完整：JPEG 数据缺少结束标记（FFD9），基本可以确定被截断",
                "请重新获取/另存这张图；截断的图即使能解码，边缘也会出现灰块",
            )
    elif data[:8] == b"\x89PNG\r\n\x1a\n":             # PNG: ... IEND
        if b"IEND" not in data[-32:]:
            raise CompatError(
                "图片文件不完整：PNG 数据缺少 IEND 结束块，基本可以确定被截断",
                "请重新获取/另存这张图",
            )


# ------------------------------------------------------------------ 解码与规范化

def decode_image(data: bytes) -> tuple[np.ndarray, list[str], dict]:
    """把上传字节解成 BGR uint8，返回 (图, 警告列表, 元信息)。"""
    check_integrity(data)
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise CompatError(
            "无法解析这张图片：文件可能已损坏，或不是常见图片格式",
            "支持 JPG / PNG / BMP / WebP / TIFF；可先用系统看图工具另存一次再试",
        )

    warnings: list[str] = []

    # 16 位 / 浮点图：缩放到 8 位，否则算法拒绝
    if img.dtype != np.uint8:
        if np.issubdtype(img.dtype, np.floating):
            img = np.clip(img * 255.0 if img.max() <= 1.0 else img, 0, 255)
        else:
            img = img.astype(np.float64) / max(float(img.max()), 1.0) * 255.0
        img = np.rint(img).astype(np.uint8)
        warnings.append("检测到高位深图片，已自动转换为 8 位处理（会丢失原始位深）")

    channels_in = 1 if img.ndim == 2 else img.shape[2]

    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        warnings.append("灰度图：已转为三通道后处理，水印仍嵌在亮度通道")
    elif channels_in == 4:
        # 透明图：按白底合成。不合成的话 alpha=0 区域颜色无意义，Y 通道算不准
        alpha = img[:, :, 3:4].astype(np.float64) / 255.0
        rgb = img[:, :, :3].astype(np.float64)
        merged = rgb * alpha + 255.0 * (1.0 - alpha)
        img = np.rint(merged).astype(np.uint8)
        warnings.append("透明图片（含 Alpha 通道）：已按白色背景合成后嵌入，输出为不透明 PNG")
    elif channels_in == 1:
        img = cv2.cvtColor(img[:, :, 0], cv2.COLOR_GRAY2BGR)
        warnings.append("单通道图片：已转为三通道后处理")
    elif channels_in != 3:
        raise CompatError("暂不支持这种通道格式的图片", "请另存为 JPG 或 PNG 后再试")

    h, w = img.shape[:2]
    if max(h, w) > config.COMPAT_MAX_SIDE:
        raise CompatError(
            f"图片尺寸过大（{w}×{h}，长边上限 {config.COMPAT_MAX_SIDE}）",
            "演示阶段请先缩小到长边 5000 像素以内，避免单次嵌入耗时过长",
        )

    blocks = (h // 8) * (w // 8)
    if blocks < config.COMPAT_MIN_BLOCKS:
        raise CompatError(
            f"图片太小：{w}×{h} 只有 {blocks} 个 8×8 块，"
            f"至少要 {config.COMPAT_MIN_BLOCKS} 个（约 96×96 像素）",
            "换一张 ≥96×96 的图；常规照片建议长边 ≥1000，冗余度才够抗压缩",
        )

    # 算法只按 8×8 块工作、不校验长宽比，200×40 这种极扁图也能"通过"。
    # 必须在业务层挡住，否则用户会拿到一张又扁又脆的嵌入结果。
    if min(h, w) < config.COMPAT_MIN_SIDE:
        raise CompatError(
            f"图片太窄或太扁（{w}×{h}，最短边 {min(h, w)}px，"
            f"下限 {config.COMPAT_MIN_SIDE}px）",
            "请先裁剪成接近常规比例，或换一张图；极扁图即使能嵌，视觉与鲁棒性都不可靠",
        )

    # 近乎纯色的图：算法能嵌，但块状噪点在纯色区域肉眼可见
    lap_var = float(cv2.Laplacian(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())
    if lap_var < config.COMPAT_LOW_TEXTURE_VAR:
        warnings.append(
            f"这张图几乎没有纹理（Laplacian 方差 {lap_var:.2f}）：水印能嵌进去，"
            "但块状噪点在纯色区域肉眼可见，实际业务不建议使用"
        )

    repeats = int(blocks // 32)   # 32 bit 载荷
    if repeats < config.COMPAT_LOW_REPEAT:
        warnings.append(
            f"图片偏小：每个 bit 只有 {repeats} 票冗余（常规照片 900+ 票），"
            "抗压缩能力明显更弱，建议不要作为演示主样本"
        )

    meta = {
        "width": w,
        "height": h,
        "channels": int(channels_in),
        "blocks": int(blocks),
        "repeats": repeats,
        "min_side": int(min(h, w)),
        "laplacian_var": round(lap_var, 2),
    }
    return img, warnings, meta


# ------------------------------------------------------------------ 画质指标

def ssim(a: np.ndarray, b: np.ndarray) -> float:
    """与 A 组原实现逐行一致（Wang 版、灰度 + 11×11 高斯窗 σ=1.5）。

    保持同一实现是为了让本服务的 SSIM 与 A 组测试记录可比；
    换实现会引入系统偏差，属于不可接受的口径变化。
    """
    x = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY).astype(np.float64)
    y = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY).astype(np.float64)
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    mx = gaussian_filter(x, 1.5, truncate=3.5)
    my = gaussian_filter(y, 1.5, truncate=3.5)
    mxx = gaussian_filter(x * x, 1.5, truncate=3.5)
    myy = gaussian_filter(y * y, 1.5, truncate=3.5)
    mxy = gaussian_filter(x * y, 1.5, truncate=3.5)
    vx, vy, vxy = mxx - mx * mx, myy - my * my, mxy - mx * my
    m = ((2 * mx * my + c1) * (2 * vxy + c2)) / ((mx ** 2 + my ** 2 + c1) * (vx + vy + c2))
    return float(np.clip(m.mean(), -1.0, 1.0))


# ------------------------------------------------------------------ 输出编码

def png_b64(bgr: np.ndarray) -> str:
    """BGR → Base64 PNG（无损）。嵌入结果绝不能用 JPEG 返回。"""
    ok, buf = cv2.imencode(".png", bgr)
    if not ok:
        raise CompatError("生成结果图片失败", "请换一张图再试")
    return base64.b64encode(buf.tobytes()).decode("ascii")


def preview_b64(bgr: np.ndarray, max_side: int = 900) -> str:
    """长边超过 max_side 时等比缩小，只用于页面缩略图。"""
    h, w = bgr.shape[:2]
    if w > max_side:
        nh = max(1, int(round(h * max_side / w)))
        bgr = cv2.resize(bgr, (max_side, nh), interpolation=cv2.INTER_AREA)
    return png_b64(bgr)


def jpeg_roundtrip(bgr: np.ndarray, qf: int) -> tuple[bytes, np.ndarray]:
    """真实 JPEG 有损压缩往返：编码一次，字节与解码结果一起返回。

    A 组原实现里 `_jpeg_b64()` 与 `jpeg_attack()` 各编码一次（同一结果算两遍）；
    这里编码一次复用字节，输出等价、少一次 CPU。
    """
    ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(qf)])
    if not ok:
        raise CompatError(f"JPEG 压缩失败（QF={qf}）", "请换一张图或换一档质量再试")
    raw = buf.tobytes()
    out = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if out is None:
        raise CompatError(f"JPEG 解码失败（QF={qf}）", "请换一张图再试")
    return raw, out
