# -*- coding: utf-8 -*-
"""统一响应 / multipart 解析 —— 全部标准库。"""
from __future__ import annotations

import datetime
import json
import re
import uuid

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


def request_id() -> str:
    return uuid.uuid4().hex[:12]


def ts() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def payload(ok: bool, data=None, error=None, rid=None) -> bytes:
    body = {"ok": ok, "data": data if ok else None,
            "error": None if ok else error,
            "meta": {"request_id": rid or request_id(), "ts": ts()}}
    # allow_nan=False：禁止 Infinity / NaN 进入 JSON（psnr 的 inf 已在适配层清洗）
    return json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")


def ok(data, rid=None) -> bytes:
    return payload(True, data=data, rid=rid)


def err(code: str, message: str, rid=None) -> bytes:
    return payload(False, error={"code": code, "message": message}, rid=rid)


_NAME_RE = re.compile(r'name="([^"]*)"')


def parse_multipart(body: bytes, content_type: str):
    """用 email.parser (标准库) 解析 multipart/form-data，兼容 Python 3.13（cgi 已移除）。

    返回 (fields: dict[str,str], files: dict[str, dict])；解析失败抛 ValueError。
    """
    from email.parser import BytesParser
    from email.policy import default

    if "multipart/form-data" not in (content_type or "").lower():
        raise ValueError("Content-Type 必须为 multipart/form-data")
    raw = (b"MIME-Version: 1.0\r\nContent-Type: "
           + content_type.encode("latin-1", "replace") + b"\r\n\r\n" + body)
    msg = BytesParser(policy=default).parsebytes(raw)
    if not msg.is_multipart():
        raise ValueError("multipart 边界缺失或格式错误")
    fields, files = {}, {}
    for part in msg.iter_parts():
        cd = part.get("Content-Disposition", "") or ""
        m = _NAME_RE.search(cd)
        name = m.group(1) if m else (part.get_param("name", header="content-disposition") or "")
        blob = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is not None:
            files[name] = {"filename": filename, "content": blob,
                           "content_type": part.get_content_type()}
        elif name:
            fields[name] = blob.decode("utf-8", "replace").strip()
    return fields, files
