# -*- coding: utf-8 -*-
"""TraceGuard 后端 HTTP 服务 —— 仅使用 Python 标准库 http.server。

合规：不引入任何框架、不安装任何依赖、不修改项目内任何文件。
真实性：算法依赖不可用时，embed/extract 的合法请求返回 503，绝不伪造成功结果。
"""
from __future__ import annotations

import os
import re
import time
import traceback

from http.server import BaseHTTPRequestHandler

from . import compat, config, core_adapter, http_utils, image_probe

_OUTPUT_RE = re.compile(r"^/api/v1/output/(wm_[0-9a-f]{12}\.png)$")


class Handler(BaseHTTPRequestHandler):
    server_version = "TraceGuardBackend/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # 静默：运行证据由 evidence/smoke_test.py 逐条记录，避免噪声
        pass

    # ------------------------------------------------------------ 底层发送
    def _send(self, status: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in http_utils.CORS.items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, status: int, body: bytes):
        self._send(status, body)

    def _fail(self, status: int, code: str, message: str):
        self._json(status, http_utils.err(code, message))

    # ------------------------------------------------------------ 方法分派
    def do_OPTIONS(self):
        self._send(204, b"")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        rid = http_utils.request_id()
        try:
            if path == "/":
                return self._get_index(rid)
            # ---- 兼容层新增：页面托管 / 存活探测 / 静态资源 ----
            if path in ("/app", "/app/"):
                return self._send_ui()
            if path == "/health":
                return compat.health(self)
            if path.startswith("/static/"):
                return self._send_static(path)
            # ---- B 组原有 /api/v1/* ----
            if path == "/api/v1/health":
                return self._get_health(rid)
            if path == "/api/v1/info":
                return self._get_info(rid)
            m = _OUTPUT_RE.match(path)
            if m:
                return self._get_output(m.group(1), rid)
            if path in ("/api/v1/embed", "/api/v1/extract"):
                return self._fail(405, "METHOD_NOT_ALLOWED", "该端点仅支持 POST")
            return self._fail(404, "NOT_FOUND", "未知路径: " + path)
        except Exception as e:
            return self._internal(e)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        rid = http_utils.request_id()
        try:
            # ---- 兼容层新增：A 组四个接口（JSON + Base64，恒 200） ----
            if path in compat.ROUTES:
                return compat.dispatch(self, path)
            # ---- B 组原有 /api/v1/* ----
            if path == "/api/v1/embed":
                return self._post_embed(rid)
            if path == "/api/v1/extract":
                return self._post_extract(rid)
            if path in ("/api/v1/health", "/api/v1/info", "/"):
                return self._fail(405, "METHOD_NOT_ALLOWED", "该端点仅支持 GET")
            return self._fail(404, "NOT_FOUND", "未知路径: " + path)
        except Exception as e:
            return self._internal(e)

    def _internal(self, e):
        # 不向前端泄露堆栈，但如实记录类型（日志侧）
        return self._fail(500, "INTERNAL_ERROR", f"{type(e).__name__}: {e}")

    # ------------------------------------------------------------ GET 端点
    @staticmethod
    def _runtime_mode(probe):
        """运行模式取自运行时依赖探测，避免静态描述与实际能力脱节。"""
        return config.MODE_CORE if probe["available"] else config.MODE_FALLBACK

    def _get_index(self, rid):
        # 这里只列出接口清单；验收/运行状态一律以 /api/v1/health 的动态字段为准，
        # 不再写入整合阶段的历史静态结论，避免误导为「算法未运行」。
        probe = core_adapter.probe_algorithm()
        data = {
            "service": config.SERVICE, "version": config.VERSION,
            "mode": self._runtime_mode(probe),
            "ui_url": config.UI_ENTRY,
            "status_note": "运行时算法状态请以 /api/v1/health 的 algorithm_available / core_hash_ok 为准",
            "endpoints": [
                {"method": "GET", "path": "/api/v1/health"},
                {"method": "GET", "path": "/api/v1/info"},
                {"method": "POST", "path": "/api/v1/embed"},
                {"method": "POST", "path": "/api/v1/extract"},
                {"method": "GET", "path": "/api/v1/output/<name>"},
            ],
        }
        self._json(200, http_utils.ok(data, rid))

    def _get_health(self, rid):
        import platform
        probe = core_adapter.probe_algorithm()
        data = {
            "service": config.SERVICE, "version": config.VERSION,
            "status": "ok" if probe["available"] else "degraded",
            "python": platform.python_version(),
            "frozen_interpreter": False,
            "interpreter_note": "非冻结解释器（规格环境 Python 3.11.16 本机不存在），如实标注",
            "mode": self._runtime_mode(probe),
            "algorithm_available": probe["available"],
            "algorithm_blocker": probe["blocker"],
            "missing_dependencies": probe["missing"],
            "core_file": probe["core_hash"]["path"],
            "core_hash": probe["core_hash"]["sha256_prefix"],
            "core_hash_ok": probe["core_hash"]["ok"],
            "core_hash_expected": config.CORE_SHA256_PREFIX,
        }
        self._json(200, http_utils.ok(data, rid))

    def _get_info(self, rid):
        data = {
            "core_module": "traceguard_dct.py",
            "source": "静态源码分析，见 docs/算法接口说明_traceguard_dct.md",
            "constants": {"BLOCK": 8, "COEF_A": [2, 1], "COEF_B": [1, 2],
                          "MAGIC": "0xB2", "PAYLOAD_BITS": 32,
                          "SEED": 20261109, "MAX_ITER": 24},
            "public_functions": ["crc8", "embed", "extract", "psnr"],
            "backend_callable": ["embed", "extract", "psnr"],
            "code_range": [config.CODE_MIN, config.CODE_MAX],
            "min_blocks": config.MIN_BLOCKS,
            "input_contract": ["BGR 三通道", "np.uint8", "ndim=3",
                               "(h//8)*(w//8) >= 96", "结果图必须 PNG 无损",
                               "strength/margin 未传则不注入，由算法用自身默认值"],
            "payload": "8bit 同步头 + 16bit 编号 + 8bit CRC-8",
            "limitations": [
                "不处理裁剪/缩放/旋转（会破坏 8x8 块对齐）",
                "CRC-8 是载荷完整性校验，不是身份认证",
                "哈希校验只证明文件未变，不是绝对防篡改",
                "valid=false 仅表示未检测到有效水印，不等于检测到篡改",
                "本系统是技术可行性验证 POC，不是完整商业化平台",
            ],
        }
        self._json(200, http_utils.ok(data, rid))

    def _get_output(self, name, rid):
        path = os.path.join(config.OUTPUT_DIR, name)
        if not os.path.isfile(path):
            return self._fail(404, "OUTPUT_NOT_FOUND", "结果图不存在: " + name)
        with open(path, "rb") as f:
            self._send(200, f.read(), "image/png")
        return None

    # ------------------------------------------------------------ 兼容层：页面托管
    def _send_ui(self):
        """返回 A 组页面（static/index.html，内容与 A 组原件字节一致）。"""
        path = os.path.join(config.UI_DIR, "index.html")
        if not os.path.isfile(path):
            return self._fail(404, "UI_NOT_FOUND", "页面文件不存在，请检查 webapp/static/index.html")
        with open(path, "rb") as f:
            self._send(200, f.read(), "text/html; charset=utf-8")
        return None

    def _send_static(self, path):
        """只允许 static/ 下的一层文件名，防目录穿越。"""
        name = path[len("/static/"):]
        if not name or "/" in name or "\\" in name or name.startswith("."):
            return self._fail(404, "NOT_FOUND", "非法静态资源路径")
        root = os.path.realpath(config.UI_DIR)
        full = os.path.realpath(os.path.join(root, name))
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            return self._fail(404, "NOT_FOUND", "静态资源不存在: " + name)
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }.get(os.path.splitext(name)[1].lower(), "application/octet-stream")
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)
        return None

    # ------------------------------------------------------------ POST 公共校验
    def _read_body(self):
        ctype = self.headers.get("Content-Type", "")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length > config.MAX_UPLOAD:
            raise _BadRequest("FILE_TOO_LARGE", "上传内容超过 20 MiB 上限")
        body = self.rfile.read(length) if length else b""
        try:
            fields, files = http_utils.parse_multipart(body, ctype)
        except ValueError as e:
            raise _BadRequest("BAD_MULTIPART", str(e))
        return fields, files

    @staticmethod
    def _validate_upload(files) -> tuple[bytes, dict]:
        if "file" not in files:
            raise _BadRequest("MISSING_FILE", "缺少 file 字段")
        up = files["file"]
        blob = up["content"]
        if not blob:
            raise _BadRequest("EMPTY_FILE", "上传文件为空")
        if len(blob) > config.MAX_UPLOAD:
            raise _BadRequest("FILE_TOO_LARGE", "上传文件超过 20 MiB 上限")
        ext = os.path.splitext(up["filename"] or "")[1].lower()
        if ext not in config.ALLOWED_EXT:
            raise _BadRequest("BAD_IMAGE_EXT", "不支持的扩展名: " + (ext or "(无)"))
        meta = image_probe.probe(blob)
        if meta["format"] is None:
            raise _BadRequest("IMAGE_DECODE_FAILED", meta["note"])
        if meta["too_small"]:
            raise _BadRequest("IMAGE_DECODE_FAILED",
                              f"图像太小: 块数 {meta['n_blocks']} < {config.MIN_BLOCKS}（{meta['width']}x{meta['height']}）")
        return blob, meta

    @staticmethod
    def _validate_code(fields) -> int:
        raw = fields.get("code")
        if raw is None or raw == "":
            raise _BadRequest("INVALID_CODE", "缺少 code 字段")
        try:
            code = int(raw, 10)
        except ValueError:
            raise _BadRequest("INVALID_CODE", "code 必须是整数")
        if not (config.CODE_MIN <= code <= config.CODE_MAX):
            raise _BadRequest("INVALID_CODE", "编号必须是 0~65535 的整数")
        return code

    @staticmethod
    def _validate_optional_float(fields, key, errcode) -> float | None:
        raw = fields.get(key)
        if raw is None or raw == "":
            return None
        try:
            v = float(raw)
        except ValueError:
            raise _BadRequest(errcode, f"{key} 必须是数值")
        import math
        if not math.isfinite(v) or v <= 0:
            raise _BadRequest(errcode, f"{key} 必须是有限正数（本层只校验，不改写合法值）")
        return v

    def _algorithm_or_503(self):
        probe = core_adapter.probe_algorithm()
        if not probe["core_hash"]["ok"]:
            raise _ServiceUnavailable("CORE_HASH_MISMATCH",
                                      "核心文件哈希与冻结基线不符，拒绝调用算法")
        if not probe["available"]:
            raise _ServiceUnavailable("ALGORITHM_UNAVAILABLE", probe["blocker"])
        return probe

    # ------------------------------------------------------------ POST /embed
    def _post_embed(self, rid):
        try:
            fields, files = self._read_body()
            blob, meta = self._validate_upload(files)
            code = self._validate_code(fields)
            strength = self._validate_optional_float(fields, "strength", "INVALID_STRENGTH")
            margin = self._validate_optional_float(fields, "margin", "INVALID_MARGIN")
            self._algorithm_or_503()
        except _ServiceUnavailable as e:
            return self._fail(503, e.code, e.message)
        except _BadRequest as e:
            return self._fail(400, e.code, e.message)

        t0 = time.time()
        try:
            import numpy as np
            import cv2
            arr = np.frombuffer(blob, dtype=np.uint8)
            bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if bgr is None or bgr.ndim != 3 or bgr.dtype != np.uint8:
                raise ValueError("解码结果不满足 BGR 三通道 uint8 契约")
            out, info = core_adapter.do_embed(bgr, code, strength, margin)
            psnr = core_adapter.do_psnr(bgr, out)
            os.makedirs(config.OUTPUT_DIR, exist_ok=True)
            fname = f"wm_{rid}.png"
            # Windows + 非 ASCII 路径下 cv2.imwrite 会静默返回 False（本机实测）。
            # 改用 imencode + Python open 写入，避免 OpenCV 的路径编码限制。
            ok, buf = cv2.imencode(".png", out)
            if not ok:
                raise RuntimeError("结果图 PNG 编码失败")
            with open(os.path.join(config.OUTPUT_DIR, fname), "wb") as f:
                f.write(buf.tobytes())
            data = dict(info)
            data.update(psnr)
            data.update({"output_filename": fname, "output_url": "/api/v1/output/" + fname,
                         "elapsed_ms": round((time.time() - t0) * 1000, 1),
                         "image_meta": meta})
            return self._json(200, http_utils.ok(data, rid))
        except Exception as e:
            return self._fail(400, "IMAGE_DECODE_FAILED", f"{type(e).__name__}: {e}")

    # ------------------------------------------------------------ POST /extract
    def _post_extract(self, rid):
        try:
            _, files = self._read_body()
            blob, meta = self._validate_upload(files)
            self._algorithm_or_503()
        except _ServiceUnavailable as e:
            return self._fail(503, e.code, e.message)
        except _BadRequest as e:
            return self._fail(400, e.code, e.message)

        t0 = time.time()
        try:
            import numpy as np
            import cv2
            bgr = cv2.imdecode(np.frombuffer(blob, dtype=np.uint8), cv2.IMREAD_COLOR)
            if bgr is None or bgr.ndim != 3 or bgr.dtype != np.uint8:
                raise ValueError("解码结果不满足 BGR 三通道 uint8 契约")
            result = core_adapter.do_extract(bgr)
            result["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
            result["image_meta"] = meta
            return self._json(200, http_utils.ok(result, rid))
        except Exception as e:
            return self._fail(400, "IMAGE_DECODE_FAILED", f"{type(e).__name__}: {e}")


class _HttpError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


class _BadRequest(_HttpError):
    """400 类：请求/输入契约错误。"""


class _ServiceUnavailable(_HttpError):
    """503 类：环境或完整性前置条件不满足。

    与 _BadRequest 平级（互不继承），避免被 except _BadRequest 先捕获而误返回 400。
    """

