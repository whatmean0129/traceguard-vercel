# -*- coding: utf-8 -*-
"""Vercel Python Function 入口：把 webapp 的 Handler 原样挂成 serverless handler。

本文件是 deploy_vercel/ 中唯一新增的 Python 代码；webapp/ 与 traceguard_dct.py
均为原样复制，一行未改。它只做五件事：

  1. 把 deploy_vercel/ 与 deploy_vercel/webapp/ 加入 sys.path，
     让 `from app import server` 与 `import traceguard_dct` 可解析；
  2. 暴露顶层 `handler`（BaseHTTPRequestHandler 子类）供 Vercel 调用；
  3. 还原 vercel.json rewrites 注入的 __path，保持原有 8 类路由语义；
  4. 将 config 的输出/日志/上传/证据目录与 compat._LOG_DIR 指向
     /tmp/traceguard_vercel/ 下各自独立的子目录（Vercel 仅 /tmp 可写）；
  5. 兜底 compat.health() 读取的 server_port，并做 4.5 MB 响应体预检。

边界声明：
  * vercel.json 的 rewrite 目标为官方 Python 函数入口 /api；source 用
    negative lookahead 排除精确 /api，避免 catch-all 自匹配造成循环。
  * 运行时写盘统一落在 /tmp/traceguard_vercel/{output,logs,uploads,evidence}，
    只服务单次请求，不承诺跨请求持久化；
    /api/v1/output/<name> 在本形态下仅「单实例内有效」。
  * 响应体预检：应用层主动返回 HTTP 413 + 小体积 JSON，避免被 Vercel
    平台层以「无 JSON 的 413」截断；原始前端 post() 只做 r.json()，
    不检查 Response.ok，因此仍能解析并显示 {ok:false,error,hint}。
  * 超限请求体由平台层直接 413，应用层收不到请求、无法拦截，
    需在上传前控制图片大小。
  * 本部署为技术演示环境，公网数值不得作为 Phase1-3 实验结论。
"""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # deploy_vercel/
WEBAPP = os.path.join(ROOT, "webapp")
for _p in (ROOT, WEBAPP):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Vercel 文件系统只读（/tmp 除外）：禁止解释器写 __pycache__
sys.dont_write_bytecode = True

from app import compat, config, server  # noqa: E402

# ---- 方案①：4.5 MB 响应体预检（保留原始前端，前端零改动） ----
# Vercel Hobby 平台层请求/响应体上限 4.5 MB。取 4_400_000 bytes（约 4.2 MiB）
# 留出余量，同时覆盖 4.5 MB 的十进制/二进制两种换算口径。
MAX_RESPONSE_BODY = 4_400_000
_LIMIT_ERROR = "结果图超过 Vercel 4.5 MB 上限，请换长边 ≤1500 的图"
_LIMIT_HINT = (
    "公网免费版仅支持中小图；本地整合版 webapp/ 无此限制。"
    "此提示由部署适配层给出，不是水印算法失败，也不是实验结果。"
)

# ---- 运行时写盘点：Vercel 只读文件系统，仅 /tmp 可写 ----
# 官方 Runtimes 文档：read-only filesystem with writable /tmp (up to 500 MB)。
# 不再用 tempfile.gettempdir()（其结果依赖 TMPDIR，可能指向只读路径）；
# 四类文件拆到 /tmp 下各自独立的子目录，避免同名互相覆盖，也便于清理。
_RUNTIME_ROOT = "/tmp/traceguard_vercel"
_DIR_OUTPUT = _RUNTIME_ROOT + "/output"
_DIR_LOG = _RUNTIME_ROOT + "/logs"
_DIR_UPLOAD = _RUNTIME_ROOT + "/uploads"
_DIR_EVIDENCE = _RUNTIME_ROOT + "/evidence"
_RUNTIME_DIRS = (_DIR_OUTPUT, _DIR_LOG, _DIR_UPLOAD, _DIR_EVIDENCE)

config.OUTPUT_DIR = _DIR_OUTPUT
config.LOG_DIR = _DIR_LOG
config.UPLOAD_DIR = _DIR_UPLOAD
config.EVIDENCE_DIR = _DIR_EVIDENCE
compat._LOG_DIR = _DIR_LOG


def _prepare_runtime_dirs():
    """冷启动时创建 /tmp 下四个独立子目录；失败不阻断函数导入。

    业务层本身也有 os.makedirs（server._post_embed / compat._log），
    这里统一兜底 uploads / evidence 两个当前未主动创建的目录。
    """
    for d in _RUNTIME_DIRS:
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass  # /tmp 不可写时交由业务层如实报错，不在此伪造成功


# 仅 Linux（Vercel 运行时）落盘创建；本地 Windows 纯导入自检不产生副作用。
if os.name == "posix":
    _prepare_runtime_dirs()


class _ServerProxy:
    """Vercel 的 mock server 可能没有 server_port 属性。

    只兜底 compat.health() 读取的这一个属性，其余属性原样透传。
    """

    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)

    @property
    def server_port(self):
        return getattr(object.__getattribute__(self, "_inner"), "server_port", 0)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_inner"), name)


class VercelHandler(server.Handler):
    """只加适配层，不改 do_GET / do_POST / do_OPTIONS 的业务分派逻辑。"""

    def _adapt(self):
        # ① 还原 __path：vercel.json 的 catch-all rewrite 显式传入原路径，
        #    不依赖 Vercel 内部是否改写 self.path。
        p = self.path or "/"
        if "__path=" in p:
            try:
                vals = parse_qs(urlsplit(p).query, keep_blank_values=True).get("__path")
                if vals:
                    orig = vals[0]
                    self.path = orig if orig.startswith("/") else "/" + orig
            except Exception:
                pass  # 还原失败则保留平台给的 path，不伪造成功
        # ② compat.health() 的 server_port 兜底
        inner = getattr(self, "server", None)
        if not isinstance(inner, _ServerProxy):
            self.server = _ServerProxy(inner)

    def do_GET(self):
        self._adapt()
        return super().do_GET()

    def do_POST(self):
        self._adapt()
        return super().do_POST()

    def do_OPTIONS(self):
        self._adapt()
        return super().do_OPTIONS()

    def _send(self, status, body, ctype="application/json; charset=utf-8"):
        # ③ 响应体预检：超限时主动返回 HTTP 413 + 兼容层契约内的合法 JSON。
        #    替换后的 body 远小于 4.5 MB，不会再被平台层截断；
        #    原始 index.html 的 post() 直接 r.json()（不检查 Response.ok），
        #    所以 !res.ok 分支仍会显示 error / hint。
        #    只有本分支改写 status；其余响应一律原样透传，
        #    绝不会把原始业务响应的 HTTP 状态码改成 200。
        if body and len(body) > MAX_RESPONSE_BODY:
            body = json.dumps(
                {"ok": False, "error": _LIMIT_ERROR, "hint": _LIMIT_HINT},
                ensure_ascii=False,
            ).encode("utf-8")
            ctype = "application/json; charset=utf-8"
            status = 413
        return super()._send(status, body, ctype)


# Vercel /api/*.py 约定：顶层 handler 必须是 BaseHTTPRequestHandler 子类。
#
# 构建期采用静态分析识别入口：仅写 `handler = VercelHandler` 时，其声明的
# 基类是 server.Handler，静态分析无法证明它继承 BaseHTTPRequestHandler，
# 会报 PYTHON_ENTRYPOINT_NOT_FOUND。因此这里定义一个顶层的 handler 类，
# 把 BaseHTTPRequestHandler 写为显式基类之一，供静态分析识别。
#
# MRO: handler -> VercelHandler -> server.Handler -> BaseHTTPRequestHandler
# 所有 do_GET / do_POST / do_OPTIONS / _send / _adapt 仍由 VercelHandler 提供，
# 运行行为与原来的 `handler = VercelHandler` 完全一致；不新增、不覆盖任何
# 业务逻辑，也不改动 413 预检与 /tmp 写盘等安全检查。
class handler(VercelHandler, BaseHTTPRequestHandler):
    pass
