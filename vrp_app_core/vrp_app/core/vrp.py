"""
VRP core math — DETERMINISTIC.

กฎเดียวกับ estimators.py: ห้าม import config, ห้ามมี calibration constant

หมายเหตุสำคัญเรื่อง forecast_rv():
ฟังก์ชันนี้รับ `weights` เป็น argument บังคับ (ไม่มี default) โดยตั้งใจ —
น้ำหนัก HAR เป็นค่าที่เราเลือก ไม่ใช่ค่าที่คณิตศาสตร์กำหนด จึงต้องมาจาก config
ตัวฟังก์ชันเองแค่ทำ weighted average + renormalize ซึ่งตายตัว
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

import numpy as np

from .estimators import EstimatorError, rv_close_to_close


class VRPError(ValueError):
    """input ไม่สมเหตุสมผล — ห้ามคืนค่าเดาแทน"""


# ---------------------------------------------------------------------------
# E[RV] forecast
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RVForecast:
    value: float                      # E[RV] annualized decimal
    windows_used: tuple[int, ...]
    weights_effective: dict[int, float]   # หลัง renormalize
    renormalized: bool
    components: dict[int, float]


def forecast_rv(close: np.ndarray, weights: Mapping[int, float]) -> RVForecast:
    """
    Blend RV หลายหน้าต่าง (HAR-inspired, Corsi 2009) เป็นประมาณการของ RV อนาคต

    VRP ที่ถูกต้องคือ IV วันนี้ เทียบ RV *อนาคต* แต่เรามีแค่ RV อดีต
    การ blend หลายหน้าต่างลด overreaction ต่อ spike เดี่ยว โดยอาศัย vol clustering

    หน้าต่างที่ข้อมูลไม่พอจะถูกตัดออกและน้ำหนักที่เหลือถูก renormalize
    (ไม่ใช่เติมศูนย์ ซึ่งจะกด E[RV] ลงอย่างผิด ๆ)
    """
    if not weights:
        raise VRPError("weights ว่าง")
    if any(w < 0 for w in weights.values()):
        raise VRPError("weights ติดลบ")

    components: dict[int, float] = {}
    for window in sorted(weights):
        try:
            components[window] = rv_close_to_close(close, window)
        except EstimatorError:
            continue  # ข้อมูลไม่พอสำหรับหน้าต่างนี้

    if not components:
        raise VRPError("ข้อมูลไม่พอสำหรับทุกหน้าต่างที่ร้องขอ")

    den = sum(weights[w] for w in components)
    if den <= 0:
        raise VRPError("น้ำหนักรวมของหน้าต่างที่ใช้ได้ = 0")

    eff = {w: weights[w] / den for w in components}
    value = sum(eff[w] * components[w] for w in components)

    return RVForecast(
        value=float(value),
        windows_used=tuple(sorted(components)),
        weights_effective=eff,
        renormalized=len(components) != len(weights),
        components=components,
    )


# ---------------------------------------------------------------------------
# §2.3 VRP
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class VRP:
    iv: float
    rv: float
    vol_points: float      # (IV - RV) * 100
    variance: float        # IV^2 - RV^2  <- ตัวที่เก็บเงินได้จริง
    ratio: float           # IV / RV
    selfcheck_residual: float

    @property
    def is_positive(self) -> bool:
        return self.ratio >= 1.0


def compute_vrp(iv: float, rv: float) -> VRP:
    """
    VRP_vol   = IV - RV
    VRP_var   = IV^2 - RV^2
    VRP_ratio = IV / RV

    ทำไมตัวหลักเป็น variance ไม่ใช่ vol:
    P&L ของ short option ที่ delta-hedge คือ
        1/2 * integral( Gamma * S^2 * (sigma_imp^2 - sigma_real^2) dt )
    เราเก็บส่วนต่างของ *variance* ถ่วงด้วย dollar gamma
    ส่วน (IV - RV) ใช้สื่อสารให้อ่านง่ายเท่านั้น

    self-check: VRP_var / (2*IV) ~= VRP_vol เมื่อ IV ใกล้ RV (first-order)
    """
    if not (math.isfinite(iv) and math.isfinite(rv)):
        raise VRPError("IV หรือ RV ไม่ใช่ตัวเลขจำกัด")
    if iv <= 0:
        raise VRPError(f"IV ต้อง > 0 (ได้ {iv})")
    if rv <= 0:
        raise VRPError(f"RV ต้อง > 0 (ได้ {rv})")

    vol_points = (iv - rv) * 100.0
    variance = iv ** 2 - rv ** 2
    ratio = iv / rv

    approx = variance / (2.0 * iv) * 100.0
    residual = abs(approx - vol_points)

    return VRP(
        iv=float(iv), rv=float(rv),
        vol_points=float(vol_points),
        variance=float(variance),
        ratio=float(ratio),
        selfcheck_residual=float(residual),
    )


# ---------------------------------------------------------------------------
# §2.4 Event decomposition
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EventDecomposition:
    iv_total: float
    iv_diffusive: float
    jump: float            # J, expected earnings-day move (decimal)
    dte: int
    total_variance: float
    event_variance: float
    diffusive_variance: float


def decompose_event(iv_total: float, dte: int, implied_move: float) -> EventDecomposition:
    """
        T = DTE/365
        total variance = IV_total^2 * T
        event variance = J^2            (จัมพ์วันเดียว ไม่ scale ด้วยเวลา)
        IV_diff = sqrt( (IV_total^2*T - J^2) / T )

    ถ้า IV_total^2*T - J^2 <= 0 -> raise
    ห้าม clamp เป็น 0 เพราะนั่นคือการซ่อนว่า input ขัดแย้งกันเอง
    (J ที่ใหญ่กว่า total variance แปลว่าตัวเลขตัวใดตัวหนึ่งผิด)
    """
    if iv_total <= 0:
        raise VRPError(f"IV_total ต้อง > 0 (ได้ {iv_total})")
    if dte <= 0:
        raise VRPError(f"DTE ต้อง > 0 (ได้ {dte})")
    if implied_move < 0:
        raise VRPError(f"implied_move ติดลบ ({implied_move})")

    t = dte / 365.0
    total_var = iv_total ** 2 * t
    event_var = implied_move ** 2
    diff_var = total_var - event_var

    if diff_var <= 0:
        raise VRPError(
            f"Event decomposition ล้มเหลว: event variance {event_var:.6f} >= "
            f"total variance {total_var:.6f}. implied_move ({implied_move:.1%}) "
            f"ใหญ่เกินกว่าที่ IV {iv_total:.1%} ที่ {dte} DTE จะรองรับได้ — "
            "ตรวจ input ทั้งสองตัว")

    return EventDecomposition(
        iv_total=float(iv_total),
        iv_diffusive=float(math.sqrt(diff_var / t)),
        jump=float(implied_move),
        dte=int(dte),
        total_variance=float(total_var),
        event_variance=float(event_var),
        diffusive_variance=float(diff_var),
    )


def earnings_vrp(implied_move: float, historical_moves: np.ndarray) -> tuple[float, float]:
    """
    Earnings VRP = implied move - median(|actual move|)
    คืน (vrp_points, median_actual) ทั้งคู่เป็นหน่วยเดียวกับ input

    นี่เป็นเกมแยกจาก diffusive VRP: วัดว่าตลาดตั้งราคา "ขนาดการกระโดด"
    แพงหรือถูกเทียบกับที่เคยกระโดดจริง
    """
    h = np.abs(np.asarray(historical_moves, dtype=float))
    if h.size == 0:
        raise VRPError("ไม่มีข้อมูล historical moves")
    if np.any(~np.isfinite(h)):
        raise VRPError("historical moves มี NaN/inf")
    median_actual = float(np.median(h))
    return float(implied_move - median_actual), median_actual


# ---------------------------------------------------------------------------
# §2.5 IV metrics
# ---------------------------------------------------------------------------
def iv_rank(iv: float, iv_min: float, iv_max: float) -> float:
    """(IV - min) / (max - min) * 100 — เปราะต่อ outlier เดียวโดยธรรมชาติ"""
    if iv_max <= iv_min:
        raise VRPError("iv_max ต้อง > iv_min")
    return float((iv - iv_min) / (iv_max - iv_min) * 100.0)


def iv_percentile(iv: float, iv_history: np.ndarray) -> float:
    """% ของวันในประวัติที่ IV ต่ำกว่าวันนี้ — ทนทานต่อ outlier มากกว่า IV Rank"""
    h = np.asarray(iv_history, dtype=float)
    h = h[np.isfinite(h)]
    if h.size == 0:
        raise VRPError("iv_history ว่าง")
    return float(np.sum(h < iv) / h.size * 100.0)


def term_structure(iv_m1: float, iv_m2: float) -> float:
    """IV_M1 / IV_M2 — <1 contango (ปกติ), >1 backwardation (ตลาดกลัวของใกล้)"""
    if iv_m1 <= 0 or iv_m2 <= 0:
        raise VRPError("IV ต้อง > 0")
    return float(iv_m1 / iv_m2)
