"""
TraceGuard POC - 基于 8x8 分块 DCT 的盲水印核心算法

原理速览:
  1. 把亮度通道 Y 切成互不重叠的 8x8 小块, 每块做 DCT, 得到 64 个频率系数。
     左上角是低频(图像主体), 右下角是高频(细节), 中间是中频。
  2. JPEG 压缩同样是 8x8 DCT + 量化: 高频量化步长大会被抹成 0, 低频改动肉眼明显,
     所以水印只嵌在中频。这里选 (2,1) 与 (1,2), 它们在 JPEG 标准亮度量化表里的
     步长分别是 13 和 14, 非常接近, 压缩后两个系数"一起缩水", 大小关系不易翻转。
  3. 关系编码: 用两个系数的大小关系表示 1 bit。
     bit=1 -> C_A - C_B >= +strength ; bit=0 -> C_A - C_B <= -strength
     strength 是人为挖出的"护城河", JPEG 带来的扰动小于它时, 关系不翻转。
  4. 盲提取: 不需要原图。对同样位置做 DCT, 看 C_A - C_B 的正负即可读出 bit。
  5. 冗余: 32 bit 载荷循环写满所有块, 每个 bit 得到上百张票, 提取时多数投票。
  6. 载荷 = 8bit 同步头 + 16bit 编号 + 8bit CRC-8。提取后先验同步头和 CRC,
     通过才认为编号可信; 没嵌过水印的图会被判为无效, 不会随便吐一个编号。

注意: 本阶段只考虑 JPEG 有损压缩, 不处理裁剪/缩放/旋转(会破坏 8x8 块对齐)。
"""

from __future__ import annotations

import cv2
import numpy as np

BLOCK = 8
COEF_A = (2, 1)
COEF_B = (1, 2)
MAGIC = 0xB2          # 同步头, 用来判断"这张图是否嵌过我们的水印"
PAYLOAD_BITS = 32     # 8 同步头 + 16 编号 + 8 CRC
SEED = 20261109       # 固定伪随机种子, 决定水印分散到哪些块; 嵌入/提取必须一致
MAX_ITER = 24         # 单块闭环修正的最大迭代次数


# ---------------------------------------------------------------- 载荷编解码

def crc8(data: bytes) -> int:
    """CRC-8 (多项式 0x07, 初值 0)。逐字节异或 + 逐位移位, 就是一种校验和。"""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _payload_bits(code: int) -> np.ndarray:
    """把 16bit 编号打包成 32bit 比特流 (高位在前)。"""
    if not 0 <= code <= 0xFFFF:
        raise ValueError("编号必须是 0~65535 的整数")
    header = bytes([MAGIC, (code >> 8) & 0xFF, code & 0xFF])
    packet = header + bytes([crc8(header)])
    return np.unpackbits(np.frombuffer(packet, dtype=np.uint8), bitorder="big")


def _parse_payload(bits: np.ndarray) -> tuple[bool, bool, int]:
    """解包并校验。返回 (同步头是否正确, CRC 是否正确, 编号)。"""
    raw = np.packbits(bits.astype(np.uint8), bitorder="big").tobytes()
    magic, hi, lo, crc = raw[0], raw[1], raw[2], raw[3]
    return magic == MAGIC, crc8(bytes([magic, hi, lo])) == crc, (hi << 8) | lo


# ---------------------------------------------------------------- DCT 块工具

def _block_positions(shape: tuple[int, ...]) -> list[tuple[int, int]]:
    """列出所有完整 8x8 块的 (行号, 列号)。不足 8 的边缘直接不参与, 避免补边引入复杂度。"""
    h, w = shape[:2]
    return [(r, c) for r in range(h // BLOCK) for c in range(w // BLOCK)]


def _effective_y(y_blk: np.ndarray, cr_blk: np.ndarray, cb_blk: np.ndarray) -> np.ndarray:
    """
    关键工程细节: 图片以 BGR 形式保存, 提取端读到的是 BGR->YCrCb 反算出来的 Y。
    YCrCb<->BGR 是逐像素运算且带取整, 往返会有 +-1 误差。所以嵌入时必须在
    "最终能被观测到的 Y" 上做闭环验证, 否则保存一次图片水印就坏了。
    颜色转换是逐像素独立的, 对单个 8x8 小块做转换与对整图做转换结果一致, 因此很便宜。
    """
    ycc = cv2.merge([y_blk, cr_blk, cb_blk])
    bgr = cv2.cvtColor(ycc, cv2.COLOR_YCrCb2BGR)
    return cv2.split(cv2.cvtColor(bgr, cv2.COLOR_BGR2YCrCb))[0].astype(np.float64)


def _diff(y_blk: np.ndarray, cr_blk: np.ndarray, cb_blk: np.ndarray) -> float:
    """读出该块携带的"带符号护城河宽度": C_A - C_B。正数代表 1, 负数代表 0。"""
    c = cv2.dct(_effective_y(y_blk, cr_blk, cb_blk))
    return float(c[COEF_A] - c[COEF_B])


def _perm(n_blocks: int) -> np.ndarray:
    """伪随机打乱块顺序, 让水印均匀散布全图, 而不是集中在前几行。"""
    return np.random.default_rng(SEED).permutation(n_blocks)


# ---------------------------------------------------------------- 嵌入

def embed(bgr: np.ndarray, code: int, strength: float = 16.0,
          margin: float = 8.0) -> tuple[np.ndarray, dict]:
    """
    在 BGR 图像的 Y 通道嵌入编号, 返回 (含水印的 BGR 图, 调试信息)。

    strength: 护城河宽度, 越大越抗压缩, 但图像失真越大。
    margin:   迭代时多推一点余量, 用来抵消 uint8 取整的损失。
    """
    if bgr.ndim != 3 or bgr.dtype != np.uint8:
        raise ValueError("需要 uint8 的三通道 BGR 图像")
    ycc = cv2.cvtColor(bgr, cv2.COLOR_BGR2YCrCb)
    Y, Cr, Cb = cv2.split(ycc)

    bits = _payload_bits(code)
    n_bits = len(bits)
    positions = _block_positions(Y.shape)
    n_blocks = len(positions)
    if n_blocks < n_bits * 3:
        raise ValueError(f"图像太小: 只有 {n_blocks} 个块, 至少需要 {n_bits * 3} 个")

    perm = _perm(n_blocks)
    expected = np.empty(n_blocks, dtype=np.uint8)  # 仅供实验统计误码率, 提取端不需要
    Yw = Y.copy()
    failed = 0

    for k, idx in enumerate(perm):
        r, c = positions[idx]
        sl = (slice(r * BLOCK, (r + 1) * BLOCK),
              slice(c * BLOCK, (c + 1) * BLOCK))
        bit = int(bits[k % n_bits])
        expected[k] = bit

        y_blk = Yw[sl].copy()
        cr_blk, cb_blk = Cr[sl], Cb[sl]
        sign = 1.0 if bit else -1.0
        target = sign * (strength + margin)
        ok = False

        for _ in range(MAX_ITER):
            y_eff = _effective_y(y_blk, cr_blk, cb_blk)
            c_mat = cv2.dct(y_eff)
            diff = c_mat[COEF_A] - c_mat[COEF_B]
            if sign * diff >= strength:       # 在最终可观测的 Y 上达标, 收工
                ok = True
                break
            # 对称地一加一减: 保持两个系数之和不变(局部能量不变), 视觉失真更小
            delta = target - diff
            c_mat[COEF_A] += delta / 2.0
            c_mat[COEF_B] -= delta / 2.0
            # 只把"改动量"加回空域, 再取整回 uint8; 下一轮重新验证, 不够就继续推
            delta_spatial = cv2.idct(c_mat) - y_eff
            y_blk = np.clip(np.rint(y_blk.astype(np.float64) + delta_spatial),
                            0, 255).astype(np.uint8)
            target += sign * 3.0

        if not ok:
            failed += 1
        Yw[sl] = y_blk

    out = cv2.cvtColor(cv2.merge([Yw, Cr, Cb]), cv2.COLOR_YCrCb2BGR)
    info = {
        "code": code, "strength": strength, "margin": margin,
        "n_blocks": n_blocks, "n_bits": n_bits,
        "repeats": n_blocks // n_bits, "failed_blocks": failed,
        "expected_bits": expected,
    }
    return out, info


# ---------------------------------------------------------------- 提取

def extract(bgr: np.ndarray, expected_bits: np.ndarray | None = None) -> dict:
    """
    盲提取: 只给一张图, 不需要原图, 也不需要知道原本嵌的是什么编号。
    expected_bits 仅供实验统计块级误码率, 真实使用时传 None。
    """
    ycc = cv2.cvtColor(bgr, cv2.COLOR_BGR2YCrCb)
    Y, Cr, Cb = cv2.split(ycc)
    positions = _block_positions(Y.shape)
    n_blocks = len(positions)
    if n_blocks < PAYLOAD_BITS * 3:
        return {"valid": False, "reason": "图像块数不足"}

    perm = _perm(n_blocks)
    votes = np.zeros(PAYLOAD_BITS, dtype=np.int32)
    counts = np.zeros(PAYLOAD_BITS, dtype=np.int32)
    diffs = np.empty(n_blocks, dtype=np.float64)
    raw_errors = 0

    for k, idx in enumerate(perm):
        r, c = positions[idx]
        sl = (slice(r * BLOCK, (r + 1) * BLOCK),
              slice(c * BLOCK, (c + 1) * BLOCK))
        d = _diff(Y[sl], Cr[sl], Cb[sl])
        diffs[k] = d
        bit = 1 if d > 0 else 0
        b = k % PAYLOAD_BITS
        votes[b] += bit
        counts[b] += 1
        if expected_bits is not None:
            raw_errors += int(bit != expected_bits[k])

    # 多数投票: 同一个 bit 被写了上百次, 少数被压坏的块不影响结果
    recovered = (votes * 2 > counts).astype(np.uint8)
    ratio = np.maximum(votes, counts - votes) / np.maximum(counts, 1)
    magic_ok, crc_ok, code = _parse_payload(recovered)

    return {
        "valid": bool(magic_ok and crc_ok),
        "code": code,
        "magic_ok": bool(magic_ok),
        "crc_ok": bool(crc_ok),
        "vote_agreement": float(ratio.mean()),       # 平均多数票比例, 1.0 表示完全一致
        "min_agreement": float(ratio.min()),         # 最弱那个 bit 的票比例
        "mean_abs_diff": float(np.abs(diffs).mean()),
        "raw_ber": float(raw_errors / n_blocks) if expected_bits is not None else None,
        "n_blocks": n_blocks,
    }


# ---------------------------------------------------------------- 画质指标

def psnr(a: np.ndarray, b: np.ndarray) -> float:
    """峰值信噪比 (dB)。> 40dB 通常肉眼难辨差异。"""
    mse = np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2)
    return float("inf") if mse == 0 else float(10 * np.log10(255.0 ** 2 / mse))
