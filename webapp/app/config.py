# -*- coding: utf-8 -*-
"""webapp 配置。

与 B 组原 config.py 的差别只有一处实质修改：
  CORE_DIR 不再硬编码他人机器路径，改为「环境变量优先 → 项目相对路径发现」。

其余常量保持 B 组原值，避免改变 /api/v1/* 的行为。
"""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # webapp/
PROJECT_ROOT = os.path.dirname(BASE_DIR)                                 # 项目根

UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
EVIDENCE_DIR = os.path.join(BASE_DIR, "evidence")
LOG_DIR = os.path.join(BASE_DIR, "logs")
UI_DIR = os.path.join(BASE_DIR, "static")
UI_ENTRY = "/app"


def discover_core_dir() -> str:
    """定位 traceguard_dct.py 所在目录。

    顺序：环境变量 TRACEGUARD_CORE_DIR → 项目根 → 项目根的上级 → 当前目录。
    找不到时返回一个明确的无效路径，让 health 如实报 core_hash_ok=false，
    而不是静默降级或抛异常导致服务起不来。
    """
    env = os.environ.get("TRACEGUARD_CORE_DIR")
    if env:
        return os.path.abspath(env)
    for cand in (
        PROJECT_ROOT,
        os.path.dirname(PROJECT_ROOT),
        os.getcwd(),
    ):
        if os.path.isfile(os.path.join(cand, "traceguard_dct.py")):
            return os.path.abspath(cand)
    return os.path.join(BASE_DIR, "__core_not_found__")


# 只读引用的核心项目目录：唯一用途是计算 traceguard_dct.py 的 SHA-256
CORE_DIR = discover_core_dir()
CORE_FILE = os.path.join(CORE_DIR, "traceguard_dct.py")
CORE_SHA256_PREFIX = "d4418430f2d0"   # 冻结基线前缀（12 hex）

HOST = "127.0.0.1"
PORT = int(os.environ.get("TRACEGUARD_PORT", "5000"))

# ---- /api/v1/* 原有上限（保持不变） ----
MAX_UPLOAD = 20 * 1024 * 1024
ALLOWED_EXT = {".png", ".bmp", ".jpg", ".jpeg"}
MIN_BLOCKS = 96          # 依据 traceguard_dct.py L113 / L176：32bit * 3
CODE_MIN, CODE_MAX = 0, 0xFFFF   # 依据 L49

# ---- /api/* 兼容层（阈值与 A 组原有实现一致） ----
COMPAT_MAX_UPLOAD = 40 * 1024 * 1024      # 解码后图片上限 40 MB
COMPAT_MAX_BODY = 80 * 1024 * 1024        # Base64 膨胀后的请求体上限
COMPAT_MAX_SIDE = 5000                    # 长边上限
COMPAT_MIN_BLOCKS = 96                    # 块数下限
COMPAT_MIN_SIDE = 64                      # 最短边下限
COMPAT_LOW_TEXTURE_VAR = 1.0              # Laplacian 方差低于此值 → 纯色警告
COMPAT_LOW_REPEAT = 8                     # 每 bit 票数低于此值 → 低冗余警告
COMPAT_ALLOWED_QF = (95, 90, 75, 50, 30, 20, 15, 10)

SERVICE = "traceguard-backend"
VERSION = "0.3.0-vercel"
# 运行模式不再写死：由 core_adapter.probe_algorithm() 的运行时探测结果决定。
# 依赖齐备且冻结核心文件哈希通过 -> dct-core-enabled；依赖缺失 -> stdlib-only。
# HTTP 层始终使用标准库，算法核心依赖 cv2/numpy/scipy（见 requirements.txt）。
MODE_CORE = "dct-core-enabled"
MODE_FALLBACK = "stdlib-only"
