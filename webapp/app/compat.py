# -*- coding: utf-8 -*-
"""A 组前端兼容层（/api/analyze | /api/embed | /api/extract | /api/attack）

协议
----
请求：application/json + Base64(dataURL 亦可)
响应：{"ok": true, "data": {...}} 或 {"ok": false, "error": "人话", "hint": "怎么解决"}
HTTP：恒 200（与 A 组原有契约一致；v1 端点不受影响）

边界
----
  * 只做协议编排（解析、校验、组装、错误映射），不实现任何水印算法
  * 算法调用一律经 core_adapter（含核心 SHA-256 校验）
  * analyze 的 probe 与 attack 的提取都是真实调用，不返回估算或伪造值
  * 不落盘原始图片；日志不记录 Base64、绝对路径与完整编号
"""
from __future__ import annotations

import base64
import json
import os
import time
import traceback
from datetime import datetime

from . import config, core_adapter

# 依赖缺失时不让整个服务起不来：兼容端点会诚实返回「算法环境未就绪」
try:
    from . import image_ops
    _IMAGE_OPS_ERROR = None
except Exception as _e:                       # pragma: no cover - 环境异常分支
    image_ops = None
    _IMAGE_OPS_ERROR = f"{type(_e).__name__}: {_e}"

_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")


# ------------------------------------------------------------------ 响应包装

def _ok(data):
    return {"ok": True, "data": data}


def _fail(message, hint=""):
    return {"ok": False, "error": message, "hint": hint}


def _send(handler, obj):
    body = json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8")
    handler._json(200, body)
    return True


# ------------------------------------------------------------------ 提取视图

def _extract_view(bgr):
    """真实调用算法 extract，并组装 A 组需要的完整字段。

    为什么不用 core_adapter.do_extract：它在 valid=false 时只保留 valid/reason，
    会丢掉 magic_ok / crc_ok / vote_agreement，而 A 前端的结果卡要展示这些。
    这里直接调用 load_core() 返回的模块，算法实现仍然只有 traceguard_dct 一份。
    """
    t0 = time.time()
    tg = core_adapter.load_core()
    r = tg.extract(bgr)
    return {
        "valid": bool(r.get("valid", False)),
        "code": int(r.get("code", 0)),
        "magic_ok": bool(r.get("magic_ok", False)),
        "crc_ok": bool(r.get("crc_ok", False)),
        "vote_agreement": round(float(r.get("vote_agreement", 0.0)), 6),
        "min_agreement": round(float(r.get("min_agreement", 0.0)), 6),
        "mean_abs_diff": round(float(r.get("mean_abs_diff", 0.0)), 4),
        "n_blocks": int(r.get("n_blocks", 0)),
        "elapsed_ms": int((time.time() - t0) * 1000),
    }


# ------------------------------------------------------------------ 四个接口

def api_analyze(payload: dict) -> dict:
    bgr, warnings, meta = image_ops.decode_image(
        image_ops.b64_to_bytes(payload.get("image"))
    )
    probe = _extract_view(bgr)          # 真实提取，未嵌图必然 valid=false
    return {
        "meta": meta,
        "warnings": warnings,
        "preview": image_ops.preview_b64(bgr),
        "embed_available": meta["blocks"] >= config.COMPAT_MIN_BLOCKS,
        "probe": probe,
        "note": "探测结果：未嵌入水印的图片会被判为无效（valid=false），不会随便给出编号",
    }


def api_embed(payload: dict) -> dict:
    bgr, warnings, meta = image_ops.decode_image(
        image_ops.b64_to_bytes(payload.get("image"))
    )
    try:
        code = int(payload.get("code"))
    except Exception:
        raise image_ops.CompatError("编号必须是 0~65535 的整数", "例如 20261")
    if not 0 <= code <= 0xFFFF:
        raise image_ops.CompatError(
            f"编号 {code} 超出范围（0~65535）", "16 bit 载荷只能表示 65536 个编号"
        )

    t0 = time.time()
    wm, info = core_adapter.do_embed(bgr, code)      # 不传 strength/margin → 用算法默认值
    elapsed = int((time.time() - t0) * 1000)

    psnr_info = core_adapter.do_psnr(bgr, wm)
    ssim_val = image_ops.ssim(bgr, wm)
    check = _extract_view(wm)

    return {
        "meta": meta,
        "warnings": warnings,
        "code": code,
        "watermarked": image_ops.png_b64(wm),
        "psnr_db": psnr_info["psnr_db"],
        "ssim": round(float(ssim_val), 6),
        "failed_blocks": int(info["failed_blocks"]),
        "n_blocks": int(info["n_blocks"]),
        "repeats": int(info["repeats"]),
        "elapsed_ms": elapsed,
        "roundtrip": check,
        "roundtrip_ok": bool(check["valid"] and check["code"] == code),
    }


def api_extract(payload: dict) -> dict:
    bgr, warnings, meta = image_ops.decode_image(
        image_ops.b64_to_bytes(payload.get("image"))
    )
    r = _extract_view(bgr)
    expect = payload.get("expect_code")
    if expect is not None and r["valid"]:
        try:
            r["match_expect"] = int(expect) == r["code"]
        except Exception:
            r["match_expect"] = None
    return {"meta": meta, "warnings": warnings, "extract": r}


def api_attack(payload: dict) -> dict:
    bgr, warnings, meta = image_ops.decode_image(
        image_ops.b64_to_bytes(payload.get("image"))
    )
    try:
        qf = int(payload.get("qf"))
    except Exception:
        raise image_ops.CompatError(
            "JPEG 质量参数不正确",
            "可选值：" + ", ".join(map(str, config.COMPAT_ALLOWED_QF)),
        )
    if qf not in config.COMPAT_ALLOWED_QF:
        raise image_ops.CompatError(
            f"不支持的质量档位 QF={qf}",
            "可选值：" + ", ".join(map(str, config.COMPAT_ALLOWED_QF)),
        )

    t0 = time.time()
    raw, attacked = image_ops.jpeg_roundtrip(bgr, qf)   # 真实有损压缩，编码一次复用字节
    r = _extract_view(attacked)
    elapsed = int((time.time() - t0) * 1000)

    return {
        "meta": meta,
        "warnings": warnings,
        "qf": qf,
        "attacked": base64.b64encode(raw).decode("ascii"),
        "extract": r,
        "elapsed_ms": elapsed,
        "note": "这是真实的 JPEG 有损压缩（非模拟），水印能否存活以提取结果为准",
    }


ROUTES = {
    "/api/analyze": api_analyze,
    "/api/embed": api_embed,
    "/api/extract": api_extract,
    "/api/attack": api_attack,
}


# ------------------------------------------------------------------ 健康探测

def health(handler):
    """A 组 _net.py / smoke_test.py 只认 ok 字段，这里给最小存活响应。"""
    body = json.dumps(
        {"ok": True, "service": "TraceGuard 兼容服务", "port": handler.server.server_port},
        ensure_ascii=False,
    ).encode("utf-8")
    handler._json(200, body)
    return True


# ------------------------------------------------------------------ 错误映射

def _runtime_error(e: RuntimeError):
    """把 core_adapter 抛出的 RuntimeError 翻译成用户能看懂的两句话。"""
    s = str(e)
    if "哈希" in s or "hash" in s.lower():
        return (
            "核心算法文件校验未通过，已拒绝调用",
            "traceguard_dct.py 可能被修改，请核对 SHA-256 是否为 d4418430f2d0 开头",
        )
    if "依赖" in s:
        return (
            "算法环境未就绪：" + s,
            "请用 conda traceguard 环境（Python 3.11）启动服务",
        )
    return ("算法环境未就绪", "请检查 traceguard_dct.py 与 cv2/numpy 是否可用")


# ------------------------------------------------------------------ 日志（脱敏）

def _log(action, ok, elapsed_ms, error=None, extra=None):
    """只记操作元数据：不记图片、Base64、绝对路径、完整编号。"""
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        row = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "action": action,
            "ok": bool(ok),
            "elapsed_ms": int(elapsed_ms),
        }
        if error:
            row["error"] = str(error)[:200]
        if extra:
            row.update(extra)
        path = os.path.join(_LOG_DIR, f"compat-{datetime.now():%Y-%m-%d}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass    # 日志失败绝不影响主流程


def _log_extra(action, data):
    """从响应里提取可安全记录的摘要字段（不含编号数值、不含图片）。"""
    extra = {}
    if action == "embed":
        extra["roundtrip_ok"] = bool(data.get("roundtrip_ok"))
        extra["failed_blocks"] = data.get("failed_blocks")
        extra["psnr_db"] = data.get("psnr_db")
    elif action == "extract":
        e = data.get("extract") or {}
        extra["valid"] = bool(e.get("valid"))
        extra["vote_agreement"] = e.get("vote_agreement")
    elif action == "attack":
        e = data.get("extract") or {}
        extra["qf"] = data.get("qf")
        extra["valid"] = bool(e.get("valid"))
        extra["vote_agreement"] = e.get("vote_agreement")
    elif action == "analyze":
        p = data.get("probe") or {}
        extra["probe_valid"] = bool(p.get("valid"))
        extra["blocks"] = (data.get("meta") or {}).get("blocks")
    return extra


# ------------------------------------------------------------------ 分发

def dispatch(handler, path):
    """处理 /api/* 请求；永远返回合法 JSON，HTTP 恒 200。"""
    action = path.rsplit("/", 1)[-1]
    t0 = time.time()
    try:
        try:
            length = int(handler.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > config.COMPAT_MAX_BODY:
            msg = "请求体过大，请压缩图片后重试"
            _log(action, False, (time.time() - t0) * 1000, msg)
            return _send(handler, _fail(msg, "单张图片请控制在 40 MB 以内"))

        raw = handler.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            msg = "请求格式不是合法 JSON"
            _log(action, False, (time.time() - t0) * 1000, msg)
            return _send(handler, _fail(msg, "请刷新页面后重试"))
        if not isinstance(payload, dict):
            msg = "请求格式不是合法 JSON"
            _log(action, False, (time.time() - t0) * 1000, msg)
            return _send(handler, _fail(msg, "请刷新页面后重试"))

        if image_ops is None:
            msg = "算法环境未就绪：无法加载图像处理依赖"
            _log(action, False, (time.time() - t0) * 1000, _IMAGE_OPS_ERROR)
            return _send(handler, _fail(msg, "请用 conda traceguard 环境启动服务"))

        fn = ROUTES[path]
        data = fn(payload)
        data["action"] = action
        elapsed = (time.time() - t0) * 1000
        _log(action, True, elapsed, extra=_log_extra(action, data))
        return _send(handler, _ok(data))

    except image_ops.CompatError as e:
        _log(action, False, (time.time() - t0) * 1000, e.message)
        return _send(handler, _fail(e.message, e.hint))
    except ValueError as e:
        # 例如算法内部的「图像太小」「编号必须是 0~65535」
        _log(action, False, (time.time() - t0) * 1000, str(e))
        return _send(handler, _fail(str(e), "请换一张图或换一个编号再试"))
    except RuntimeError as e:
        msg, hint = _runtime_error(e)
        _log(action, False, (time.time() - t0) * 1000, msg)
        return _send(handler, _fail(msg, hint))
    except Exception as e:
        print(f"[COMPAT-ERROR] {path}: {e}\n{traceback.format_exc()}", flush=True)
        msg = "处理这张图片时出现内部错误"
        _log(action, False, (time.time() - t0) * 1000, f"{type(e).__name__}: {e}")
        return _send(
            handler,
            _fail(msg, "换一张图再试；若持续出现请记录文件名与操作步骤"),
        )
