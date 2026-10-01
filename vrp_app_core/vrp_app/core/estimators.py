"""
Realized volatility estimators — DETERMINISTIC CORE.

กฎของไฟล์นี้ (§1 ของ spec):
  - ห้าม import อะไรจาก config/
  - ห้ามมี calibration constant (threshold, weight, scale ที่เราเลือกเอง)
  - ตัวเลขที่ปรากฏในไฟล์นี้ต้องเป็น "ค่าคงที่ในสูตรปิดที่ตีพิมพ์แล้ว" เท่านั้น
    และต้องมี comment อ้างที่มากำกับ

ค่าคงที่ที่อนุญาตในไฟล์นี้ และเหตุผล:
  A = 252          trading days/year (market convention)
  4*ln(2)          Parkinson (1980) normalizing constant
  0.5, (2ln2 - 1)  Garman-Klass (1980) closed-form coefficients
  0.34, 1.34       Yang-Zhang (2000) k-constant — มาจาก paper ไม่ใช่ค่าที่เราจูน

ทุกฟังก์ชันเป็น pure: input เดิม -> output เดิม, ไม่มี state, ไม่มี I/O
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

# Trading days per year. Market convention, ไม่ใช่ค่าจูน
ANNUALIZATION_DAYS: int = 252

# ความคลาดเคลื่อนสูงสุดที่ยอมรับได้สำหรับ identity check (floating point เท่านั้น)
_IDENTITY_TOL: float = 1e-9


class EstimatorError(ValueError):
    """ยกขึ้นเมื่อ input ไม่พอหรือไม่สมเหตุสมผล — ห้ามคืนค่าเดาแทน"""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def log_returns(close: np.ndarray) -> np.ndarray:
    """r_i = ln(C_i / C_{i-1}); length = len(close) - 1"""
    c = np.asarray(close, dtype=float)
    if c.ndim != 1:
        raise EstimatorError("close ต้องเป็น 1-D array")
    if c.size < 2:
        raise EstimatorError("ต้องการราคาปิดอย่างน้อย 2 จุดเพื่อคำนวณ log return")
    if np.any(~np.isfinite(c)) or np.any(c <= 0):
        raise EstimatorError("close มีค่า <= 0, NaN หรือ inf")
    return np.diff(np.log(c))


def _tail(arr: np.ndarray, window: int) -> np.ndarray:
    if window < 2:
        raise EstimatorError("window ต้อง >= 2")
    if arr.size < window:
        raise EstimatorError(f"ข้อมูลไม่พอ: ต้องการ {window} ค่า มี {arr.size}")
    return arr[-window:]


# ---------------------------------------------------------------------------
# §2.1 RV estimators
# ---------------------------------------------------------------------------
def rv_close_to_close(close: np.ndarray, window: int) -> float:
    """
    RV_cc = sqrt( (A/n) * sum(r_i^2) )

    ไม่ demean โดยตั้งใจ: สำหรับหน้าต่างสั้น drift << noise และการ demean
    เพิ่ม estimation error. นี่คือ variance ที่ delta hedge รายวันเก็บได้จริง
    จึงเป็นตัวเทียบ apples-to-apples กับ IV
    """
    r = _tail(log_returns(close), window)
    return float(math.sqrt(ANNUALIZATION_DAYS / window * float(np.sum(r ** 2))))


def rv_parkinson(high: np.ndarray, low: np.ndarray, window: int) -> float:
    """
    Parkinson (1980):
        RV = sqrt( A / (4*n*ln2) * sum( ln(H_i/L_i)^2 ) )

    ตาบอดต่อ overnight gap โดยโครงสร้าง — ประเมินต่ำในหุ้นที่ gap บ่อย
    """
    h = _tail(np.asarray(high, dtype=float), window)
    lo = _tail(np.asarray(low, dtype=float), window)
    if np.any(h <= 0) or np.any(lo <= 0):
        raise EstimatorError("High/Low มีค่า <= 0")
    if np.any(h < lo):
        raise EstimatorError("พบแถวที่ High < Low")
    hl_sq = np.log(h / lo) ** 2
    denom = 4.0 * window * math.log(2.0)  # Parkinson normalizing constant
    return float(math.sqrt(ANNUALIZATION_DAYS / denom * float(np.sum(hl_sq))))


def rv_garman_klass(
    open_: np.ndarray, high: np.ndarray, low: np.ndarray,
    close: np.ndarray, window: int,
) -> float:
    """
    Garman-Klass (1980):
        v_i = 0.5*ln(H/L)^2 - (2ln2 - 1)*ln(C/O)^2
        RV  = sqrt( (A/n) * sum(v_i) )

    0.5 และ (2ln2 - 1) เป็นสัมประสิทธิ์ใน closed form ของ GK ไม่ใช่ค่าที่เราจูน
    ยังตาบอดต่อ overnight เช่นเดียวกับ Parkinson
    """
    o = _tail(np.asarray(open_, dtype=float), window)
    h = _tail(np.asarray(high, dtype=float), window)
    lo = _tail(np.asarray(low, dtype=float), window)
    c = _tail(np.asarray(close, dtype=float), window)
    if np.any(o <= 0) or np.any(h <= 0) or np.any(lo <= 0) or np.any(c <= 0):
        raise EstimatorError("OHLC มีค่า <= 0")
    hl_sq = np.log(h / lo) ** 2
    co_sq = np.log(c / o) ** 2
    gk_co_coeff = 2.0 * math.log(2.0) - 1.0  # Garman-Klass coefficient
    v = 0.5 * hl_sq - gk_co_coeff * co_sq
    total = float(np.sum(v))
    if total <= 0:
        raise EstimatorError(
            "Garman-Klass variance <= 0 (ข้อมูลผิดปกติ: range แคบกว่า open-close move)")
    return float(math.sqrt(ANNUALIZATION_DAYS / window * total))


def rv_yang_zhang(
    open_: np.ndarray, high: np.ndarray, low: np.ndarray,
    close: np.ndarray, window: int,
) -> float:
    """
    Yang-Zhang (2000) — drift-independent, จับ overnight gap

        o_i = ln(O_i / C_{i-1})      overnight
        c_i = ln(C_i / O_i)          open-to-close
        u_i = ln(H_i / O_i)
        d_i = ln(L_i / O_i)

        sig2_o  = sum((o - obar)^2)/(n-1)
        sig2_c  = sum((c - cbar)^2)/(n-1)
        sig2_rs = sum( u(u-c) + d(d-c) )/n          Rogers-Satchell
        k       = 0.34 / (1.34 + (n+1)/(n-1))

        RV = sqrt( A * (sig2_o + k*sig2_c + (1-k)*sig2_rs) )

    ต้องการ window+1 แถว เพราะ o_i อ้างถึง close ของวันก่อนหน้า
    0.34/1.34 มาจาก Yang-Zhang paper (ค่าที่ทำให้ variance ของตัวประมาณต่ำสุด)
    """
    need = window + 1
    o_all = np.asarray(open_, dtype=float)
    h_all = np.asarray(high, dtype=float)
    l_all = np.asarray(low, dtype=float)
    c_all = np.asarray(close, dtype=float)
    sizes = {o_all.size, h_all.size, l_all.size, c_all.size}
    if len(sizes) != 1:
        raise EstimatorError("OHLC ความยาวไม่เท่ากัน")
    if o_all.size < need:
        raise EstimatorError(
            f"Yang-Zhang ต้องการ {need} แถว (window+1) มี {o_all.size}")

    o = o_all[-window:]
    h = h_all[-window:]
    lo = l_all[-window:]
    c = c_all[-window:]
    c_prev = c_all[-need:-1]

    if np.any(o <= 0) or np.any(h <= 0) or np.any(lo <= 0) or np.any(c <= 0) \
            or np.any(c_prev <= 0):
        raise EstimatorError("OHLC มีค่า <= 0")

    n = window
    if n < 3:
        raise EstimatorError("Yang-Zhang ต้องการ window >= 3")

    ov = np.log(o / c_prev)
    oc = np.log(c / o)
    u = np.log(h / o)
    d = np.log(lo / o)

    sig2_o = float(np.sum((ov - ov.mean()) ** 2) / (n - 1))
    sig2_c = float(np.sum((oc - oc.mean()) ** 2) / (n - 1))
    sig2_rs = float(np.sum(u * (u - oc) + d * (d - oc)) / n)

    k = 0.34 / (1.34 + (n + 1) / (n - 1))  # Yang-Zhang k
    var = sig2_o + k * sig2_c + (1.0 - k) * sig2_rs
    if var <= 0:
        raise EstimatorError("Yang-Zhang variance <= 0 (ข้อมูลผิดปกติ)")
    return float(math.sqrt(ANNUALIZATION_DAYS * var))


# ---------------------------------------------------------------------------
# §2.2 Semivariance
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Semivol:
    """downside/upside realized vol (annualized, decimal) + identity residual"""
    down: float
    up: float
    total: float          # = rv_close_to_close บนหน้าต่างเดียวกัน
    identity_residual: float
    n_down: int
    n_up: int
    n_zero: int


def semivariance(close: np.ndarray, window: int) -> Semivol:
    """
        RV_down^2 = (A/n) * sum( r_i^2 * 1{r_i < 0} )
        RV_up^2   = (A/n) * sum( r_i^2 * 1{r_i > 0} )

    HARD ASSERTION: RV_down^2 + RV_up^2 == RV_cc^2 (ภายใน floating point)
    ถ้าไม่ผ่าน -> raise ทันที ห้ามคืนค่าออกไปให้คนเอาไปตัดสินใจ

    หมายเหตุ: r_i == 0 ไม่เข้าทั้งสองฝั่ง และไม่กระทบ identity เพราะ 0^2 = 0
    """
    r = _tail(log_returns(close), window)
    scale = ANNUALIZATION_DAYS / window

    neg = r[r < 0]
    pos = r[r > 0]
    var_down = scale * float(np.sum(neg ** 2))
    var_up = scale * float(np.sum(pos ** 2))
    var_total = scale * float(np.sum(r ** 2))

    residual = abs(var_down + var_up - var_total)
    rel = residual / var_total if var_total > 0 else residual
    if rel > _IDENTITY_TOL:
        raise EstimatorError(
            f"Semivariance identity ล้มเหลว: down^2+up^2={var_down + var_up:.12e} "
            f"vs total^2={var_total:.12e} (rel={rel:.3e}) — คำนวณผิด")

    return Semivol(
        down=math.sqrt(var_down),
        up=math.sqrt(var_up),
        total=math.sqrt(var_total),
        identity_residual=rel,
        n_down=int(neg.size),
        n_up=int(pos.size),
        n_zero=int(np.sum(r == 0)),
    )


# ---------------------------------------------------------------------------
# gap diagnostic — คืนอัตราส่วนดิบ ไม่ตัดสิน (threshold อยู่ใน config)
# ---------------------------------------------------------------------------
def gap_ratio(rv_yz: float, rv_cc: float) -> float:
    """
    RV_yz / RV_cc — ยิ่งสูงยิ่งมี overnight risk ที่ตัวประมาณ intraday มองไม่เห็น
    ไฟล์นี้ไม่ตัดสินว่าเท่าไหร่คือ "สูง" — นั่นเป็นหน้าที่ของ config/engine
    """
    if rv_cc <= 0:
        raise EstimatorError("rv_cc ต้อง > 0")
    return rv_yz / rv_cc
