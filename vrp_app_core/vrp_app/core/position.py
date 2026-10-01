"""
Position math — DETERMINISTIC.

PoP, touch probability, expected move, expectancy, sizing
ไม่มี calibration constant (Δ range, TP%, risk% ทั้งหมดอยู่ใน config)
"""

from __future__ import annotations

import math
from dataclasses import dataclass


class PositionError(ValueError):
    pass


def expected_move(spot: float, iv: float, dte: int) -> float:
    """EM_1sd = S * IV * sqrt(DTE/365)"""
    if spot <= 0:
        raise PositionError("spot ต้อง > 0")
    if iv <= 0:
        raise PositionError("IV ต้อง > 0")
    if dte <= 0:
        raise PositionError("DTE ต้อง > 0")
    return float(spot * iv * math.sqrt(dte / 365.0))


@dataclass(frozen=True)
class Probabilities:
    pop_expiry: float       # P(short leg หมดอายุ OTM)
    p_touch: float          # P(ราคาแตะ strike ระหว่างทาง)
    delta_short: float

    @property
    def touch_penalty(self) -> float:
        """
        สัดส่วนที่ win rate จริงลดลงถ้าใช้ stop-on-touch
        p_touch ~= 2 * p_expiry_ITM จึงกินเข้าไปราวครึ่งของ edge ที่ดูเหมือนมี
        """
        return self.p_touch - (1.0 - self.pop_expiry)


def probabilities(delta_short: float) -> Probabilities:
    """
    PoP     ~= 1 - |Delta_short|      (ค่าประมาณ ณ expiry)
    P_touch ~= 2 * |Delta_short|      (reflection principle, driftless)

    สองค่านี้ต้องแสดงคู่กันเสมอ: PoP 85% ที่มี P_touch 30% แปลว่า
    ถ้าใช้ stop-on-touch จะโดนหยุดบ่อยกว่าที่ PoP บอกมาก
    """
    d = abs(float(delta_short))
    if not (0.0 < d < 1.0):
        raise PositionError(f"delta ต้องอยู่ระหว่าง 0 และ 1 (ได้ {delta_short})")
    return Probabilities(
        pop_expiry=1.0 - d,
        p_touch=min(2.0 * d, 1.0),
        delta_short=d,
    )


@dataclass(frozen=True)
class Expectancy:
    value: float
    pop: float
    avg_win: float
    avg_loss: float

    @property
    def is_positive(self) -> bool:
        return self.value > 0


def expectancy(pop: float, avg_win: float, avg_loss: float) -> Expectancy:
    """
    E = PoP * AvgWin - (1 - PoP) * AvgLoss

    ต้องแสดงคู่กับ win rate เสมอ: short put Δ0.20 ชนะ 80% ได้
    แต่ถ้า AvgLoss = 6x AvgWin แล้ว E ติดลบ
    """
    if not (0.0 <= pop <= 1.0):
        raise PositionError(f"PoP ต้องอยู่ 0..1 (ได้ {pop})")
    if avg_win < 0 or avg_loss < 0:
        raise PositionError("avg_win/avg_loss ต้องไม่ติดลบ (ใส่เป็นค่าสัมบูรณ์)")
    val = pop * avg_win - (1.0 - pop) * avg_loss
    return Expectancy(value=float(val), pop=float(pop),
                      avg_win=float(avg_win), avg_loss=float(avg_loss))


@dataclass(frozen=True)
class Sizing:
    contracts: int
    risk_budget: float
    max_loss_per_contract: float
    actual_risk_pct: float


def position_size(portfolio: float, risk_pct: float,
                  max_loss_per_contract: float) -> Sizing:
    """
    contracts = floor( portfolio * risk_pct / max_loss_per_contract )

    risk_pct มาจาก config (เป็นค่าที่เราเลือก) — ฟังก์ชันนี้แค่หาร
    คืน contracts=0 ได้ และนั่นคือคำตอบที่ถูกต้อง: ไม้นี้ใหญ่เกินพอร์ต
    """
    if portfolio <= 0:
        raise PositionError("portfolio ต้อง > 0")
    if not (0.0 < risk_pct < 1.0):
        raise PositionError(f"risk_pct ต้องอยู่ 0..1 (ได้ {risk_pct})")
    if max_loss_per_contract <= 0:
        raise PositionError("max_loss_per_contract ต้อง > 0")

    budget = portfolio * risk_pct
    n = int(budget // max_loss_per_contract)
    actual = (n * max_loss_per_contract) / portfolio if n > 0 else 0.0
    return Sizing(contracts=n, risk_budget=float(budget),
                  max_loss_per_contract=float(max_loss_per_contract),
                  actual_risk_pct=float(actual))


def credit_spread_max_loss(width: float, credit: float) -> float:
    """max loss ต่อสัญญา = (width - credit) * 100"""
    if width <= 0:
        raise PositionError("width ต้อง > 0")
    if credit < 0:
        raise PositionError("credit ติดลบ")
    if credit >= width:
        raise PositionError("credit >= width — ตรวจราคา (arbitrage หรือ input ผิด)")
    return float((width - credit) * 100.0)


def csp_max_loss(strike: float, credit: float) -> float:
    """
    Cash-secured put worst case = (K - credit) * 100 คือหุ้นไปศูนย์

    ตัวเลขนี้ดูโหดเพราะมันโหดจริง — เป็นเหตุผลที่ CSP บนหุ้น high-vol
    กินพื้นที่ความเสี่ยงมากกว่าที่คนส่วนใหญ่คิด
    """
    if strike <= 0:
        raise PositionError("strike ต้อง > 0")
    if credit < 0:
        raise PositionError("credit ติดลบ")
    return float((strike - credit) * 100.0)
