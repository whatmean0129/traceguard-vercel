# -*- coding: utf-8 -*-
"""唯一允许接触核心算法的适配层（只读）。

严格遵守《团队协作规范》：
  * 不修改、不覆盖、不重命名任何核心文件；
  * 不调整 DCT 参数 / 嵌入强度 / 判定阈值 / 载荷结构；
  * 依赖不可用时**绝不 import 核心模块**，避免在项目目录写入 __pycache__；
  * 依赖可用时也先置 sys.dont_write_bytecode，保证项目目录零写入。
"""
from __future__ import annotations

import hashlib
import importlib.util
import math
import os
import sys

from . import config

_CACHED = {"probe": None, "module": None}


def core_hash() -> dict:
    """只读计算核心文件 SHA-256，与冻结基线前缀比对。"""
    if not os.path.isfile(config.CORE_FILE):
        return {"exists": False, "sha256_prefix": None, "ok": False,
                "path": config.CORE_FILE, "expected_prefix": config.CORE_SHA256_PREFIX}
    h = hashlib.sha256()
    with open(config.CORE_FILE, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    digest = h.hexdigest()
    return {"exists": True, "sha256_prefix": digest[:12], "ok": digest.startswith(config.CORE_SHA256_PREFIX),
            "path": config.CORE_FILE, "expected_prefix": config.CORE_SHA256_PREFIX}


def probe_algorithm() -> dict:
    """只读探测算法依赖是否可用。不安装、不导入核心模块、不写字节码。"""
    if _CACHED["probe"] is not None:
        return _CACHED["probe"]
    missing = []
    for name in ("numpy", "cv2"):
        try:
            if importlib.util.find_spec(name) is None:
                missing.append(name)
        except (ImportError, ValueError):
            missing.append(name)
    result = {
        "available": not missing,
        "missing": missing,
        "blocker": None if not missing else "缺少依赖: " + ", ".join(missing)
                    + "（请检查 requirements.txt 是否参与线上构建）",
        "core_hash": core_hash(),
    }
    _CACHED["probe"] = result
    return result


def load_core():
    """仅在依赖齐备时加载核心模块；加载前置 dont_write_bytecode，保证项目目录零写入。"""
    if _CACHED["module"] is not None:
        return _CACHED["module"]
    probe = probe_algorithm()
    if not probe["available"]:
        raise RuntimeError(probe["blocker"])
    if not probe["core_hash"]["ok"]:
        raise RuntimeError("核心文件哈希不匹配冻结基线，拒绝加载")
    sys.dont_write_bytecode = True
    if config.CORE_DIR not in sys.path:
        sys.path.insert(0, config.CORE_DIR)
    import traceguard_dct  # noqa: F401  —— 只读 import，零修改
    _CACHED["module"] = traceguard_dct
    return traceguard_dct


# ---------------------------------------------------------------- 输出清洗
# 依据《算法接口说明》§3：剔除 numpy 数组与实验内部量，处理 inf，保证 JSON 严格合法。

def clean_embed_info(info: dict) -> dict:
    keep = ("code", "strength", "margin", "n_blocks", "n_bits", "repeats", "failed_blocks")
    return {k: info.get(k) for k in keep if k in info}


def clean_extract_result(r: dict) -> dict:
    """兼容 extract 的两种返回形态（L176-177 只有 2 键 / L203-213 全字段）。"""
    out = {"valid": bool(r.get("valid", False))}
    if not out["valid"]:
        out["reason"] = r.get("reason", "未检测到有效水印")
        # valid=False 时 code 无意义，不输出给前端
        return out
    for k in ("code", "magic_ok", "crc_ok", "vote_agreement",
              "min_agreement", "mean_abs_diff", "n_blocks"):
        if k in r:
            v = r[k]
            out[k] = None if isinstance(v, float) and not math.isfinite(v) else v
    return out


def clean_psnr(value) -> dict:
    if isinstance(value, float) and math.isinf(value):
        return {"psnr_db": None, "psnr_infinite": True}
    if isinstance(value, float) and math.isnan(value):
        return {"psnr_db": None, "psnr_infinite": False}
    return {"psnr_db": float(value), "psnr_infinite": False}


def do_embed(bgr, code, strength=None, margin=None):
    """调用 embed。未传的 strength/margin 一律不注入，由算法使用自身默认值。"""
    tg = load_core()
    kwargs = {}
    if strength is not None:
        kwargs["strength"] = strength
    if margin is not None:
        kwargs["margin"] = margin
    out, info = tg.embed(bgr, code, **kwargs)
    return out, clean_embed_info(info)


def do_extract(bgr):
    """调用 extract，且**不传 expected_bits**（实验内部量，业务链路禁用）。"""
    tg = load_core()
    return clean_extract_result(tg.extract(bgr))


def do_psnr(a, b):
    tg = load_core()
    if getattr(a, "shape", None) != getattr(b, "shape", None):
        raise ValueError("PSNR 输入形状不一致")
    return clean_psnr(tg.psnr(a, b))
