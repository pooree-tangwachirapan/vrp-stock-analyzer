"""
DECISION ENGINE — รวม core (ตายตัว) เข้ากับ config (จูนได้)

ไฟล์นี้เป็นที่เดียวที่ได้รับอนุญาตให้ import ทั้ง core และ config

หลักการที่ฝังอยู่ในโครงสร้าง:
1. Hard gate ตัดก่อนคะแนนเสมอ — คะแนนเต็ม 10 ก็ช่วยไม่ได้ถ้า G1 ตก
2. Gate ที่ข้อมูลไม่พอตรวจ = None (ไม่ใช่ True) และ verdict ต้องติด caveat
3. effective_ratio ใช้ Clean VRP เมื่อถอด event ได้ — นี่คือจุดที่ screener
   ทั่วไปพลาด: IV ที่มีงบปนอยู่ไม่ใช่ premium ที่ delta-hedge เก็บได้
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from ..config.defaults import VRPConfig
from ..core.estimators import Semivol, gap_ratio
from ..core.vrp import VRP, EventDecomposition


class Verdict(str, Enum):
    ENTER = "ENTER"
    HALF_SIZE = "HALF SIZE"
    WAIT = "WAIT"
    DO_NOT_ENTER = "DO NOT ENTER"
    INSUFFICIENT = "INSUFFICIENT DATA"


class Side(str, Enum):
    SHORT_PUT = "SHORT_PUT"
    SHORT_CALL = "SHORT_CALL"
    NEITHER = "NEITHER"


class GateResult(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"      # ข้อมูลไม่พอ — ไม่ใช่ PASS


@dataclass
class Gate:
    code: str
    name: str
    result: GateResult
    detail: str
    on_fail: Verdict


@dataclass
class LegVRP:
    vrp_put_pts: Optional[float]
    vrp_call_pts: Optional[float]
    rv_down_scaled: float
    rv_up_scaled: float
    realized_skew: Optional[float]
    implied_skew: Optional[float]
    skew_gap: Optional[float]
    favored: Side
    edge_gap_pts: Optional[float]
    used_atm_fallback: bool


@dataclass
class Decision:
    verdict: Verdict
    side: Side
    score: int
    max_score: int
    score_detail: list[str] = field(default_factory=list)
    gates: list[Gate] = field(default_factory=list)
    reason: str = ""
    caveats: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    effective_ratio: Optional[float] = None
    effective_ratio_source: str = "raw"
    legs: Optional[LegVRP] = None
    confidence: str = "Low"

    @property
    def failed_gates(self) -> list[Gate]:
        return [g for g in self.gates if g.result is GateResult.FAIL]

    @property
    def unknown_gates(self) -> list[Gate]:
        return [g for g in self.gates if g.result is GateResult.UNKNOWN]

    @property
    def tradeable(self) -> bool:
        return self.verdict in (Verdict.ENTER, Verdict.HALF_SIZE)


# ---------------------------------------------------------------------------
# side selection
# ---------------------------------------------------------------------------
def compute_legs(semi: Semivol, cfg: VRPConfig,
                 iv_put25: Optional[float], iv_call25: Optional[float],
                 iv_atm: float) -> LegVRP:
    """
        VRP_put  = IV(d25P) - RV_down * scale
        VRP_call = IV(d25C) - RV_up   * scale

    ถ้าไม่มี IV delta-25 -> fallback ATM ทั้งสองข้าง และตั้ง flag
    (fallback ทำให้เปรียบเทียบสองข้างไร้ความหมาย เพราะ IV เท่ากัน
     ความต่างจึงมาจาก semivol อย่างเดียว — ต้องลด confidence)
    """
    fallback = iv_put25 is None or iv_call25 is None
    ivp = iv_put25 if iv_put25 is not None else iv_atm
    ivc = iv_call25 if iv_call25 is not None else iv_atm

    down_s = semi.down * cfg.semivol_scale
    up_s = semi.up * cfg.semivol_scale

    vrp_put = (ivp - down_s) * 100.0
    vrp_call = (ivc - up_s) * 100.0

    realized_skew = semi.down / semi.up if semi.up > 0 else None
    implied_skew = ivp / ivc if ivc > 0 else None
    skew_gap = (implied_skew / realized_skew
                if implied_skew and realized_skew else None)

    if vrp_put <= 0 and vrp_call <= 0:
        favored = Side.NEITHER
    elif vrp_put >= vrp_call:
        favored = Side.SHORT_PUT
    else:
        favored = Side.SHORT_CALL

    return LegVRP(
        vrp_put_pts=vrp_put, vrp_call_pts=vrp_call,
        rv_down_scaled=down_s, rv_up_scaled=up_s,
        realized_skew=realized_skew, implied_skew=implied_skew,
        skew_gap=skew_gap, favored=favored,
        edge_gap_pts=abs(vrp_put - vrp_call),
        used_atm_fallback=fallback,
    )


# ---------------------------------------------------------------------------
# main engine
# ---------------------------------------------------------------------------
def decide(
    *,
    cfg: VRPConfig,
    vrp: VRP,
    dte: int,
    event: Optional[EventDecomposition] = None,
    semi: Optional[Semivol] = None,
    iv_put25: Optional[float] = None,
    iv_call25: Optional[float] = None,
    ivp: Optional[float] = None,
    term_struct: Optional[float] = None,
    term_struct_explained: bool = False,
    earnings_days: Optional[int] = None,
    trading_the_event: bool = False,
    spread_pct: Optional[float] = None,
    open_interest: Optional[int] = None,
    rv_yz: Optional[float] = None,
    requested_side: Optional[Side] = None,
    trend_aligned: Optional[bool] = None,
) -> Decision:
    d = Decision(verdict=Verdict.INSUFFICIENT, side=Side.NEITHER,
                 score=0, max_score=10)

    # --- effective ratio: Clean VRP ชนะ raw เสมอเมื่อถอด event ได้ ----------
    if event is not None:
        d.effective_ratio = event.iv_diffusive / vrp.rv
        d.effective_ratio_source = "clean"
        if vrp.ratio >= 1.0 > d.effective_ratio:
            d.warnings.append(
                f"VRP ดิบบวก ({vrp.ratio:.2f}) แต่ Clean VRP ติดลบ "
                f"({d.effective_ratio:.2f}) — premium ทั้งก้อนมาจาก event "
                "ไม่ใช่ diffusive edge. ขายตอนนี้คือเดิมพันขนาดการกระโดดล้วน ๆ")
    else:
        d.effective_ratio = vrp.ratio
        d.effective_ratio_source = "raw"

    # --- legs ---------------------------------------------------------------
    if semi is not None:
        d.legs = compute_legs(semi, cfg, iv_put25, iv_call25, vrp.iv)
        if d.legs.used_atm_fallback:
            d.warnings.append(
                "ไม่มี IV ของ delta-25 — ใช้ ATM IV แทนทั้งสองข้าง "
                "การเลือกข้างจึงสะท้อนเฉพาะ semivariance ไม่เห็น skew จริง")
        d.side = requested_side or d.legs.favored
    else:
        d.side = requested_side or Side.NEITHER
        d.warnings.append("ไม่มีข้อมูลราคารายวัน — คำนวณ semivariance/เลือกข้างไม่ได้")

    if rv_yz is not None and rv_yz / vrp.rv > cfg.gap_ratio_warn:
        d.warnings.append(
            f"gap_ratio (YZ/CC) = {rv_yz / vrp.rv:.2f} > {cfg.gap_ratio_warn} — "
            "ความเสี่ยงกระโดดข้ามคืนสูง: short put อาจทะลุ strike โดยไม่มีโอกาสแก้ "
            "ใช้ defined-risk และลด size")

    # --- gates --------------------------------------------------------------
    g = d.gates

    er = d.effective_ratio
    g.append(Gate("G1", "VRP positive",
                  GateResult.PASS if er >= 1.0 else GateResult.FAIL,
                  f"effective_ratio ({d.effective_ratio_source}) = {er:.3f}",
                  Verdict.DO_NOT_ENTER))

    if earnings_days is None:
        g.append(Gate("G2", "No event in DTE", GateResult.UNKNOWN,
                      "ไม่ทราบวันงบ", Verdict.WAIT))
    elif trading_the_event:
        g.append(Gate("G2", "No event in DTE", GateResult.PASS,
                      "ตั้งใจเทรด event — gate นี้ถูกข้ามโดยผู้ใช้",
                      Verdict.WAIT))
    else:
        clear = earnings_days > dte
        g.append(Gate("G2", "No event in DTE",
                      GateResult.PASS if clear else GateResult.FAIL,
                      f"งบอีก {earnings_days} วัน vs DTE {dte}", Verdict.WAIT))

    if spread_pct is None or open_interest is None:
        g.append(Gate("G3", "Liquidity", GateResult.UNKNOWN,
                      "ไม่มี bid-ask หรือ OI — ต้องเช็คก่อนส่งคำสั่งจริง",
                      Verdict.DO_NOT_ENTER))
    else:
        ok = spread_pct <= cfg.spread_max_pct and open_interest >= cfg.oi_min
        g.append(Gate("G3", "Liquidity",
                      GateResult.PASS if ok else GateResult.FAIL,
                      f"spread {spread_pct:.1f}% (max {cfg.spread_max_pct}), "
                      f"OI {open_interest} (min {cfg.oi_min})",
                      Verdict.DO_NOT_ENTER))

    if term_struct is None:
        g.append(Gate("G4", "Term structure", GateResult.UNKNOWN,
                      "ไม่มี IV M1/M2", Verdict.WAIT))
    else:
        ok = term_struct <= cfg.ts_backwardation or term_struct_explained
        g.append(Gate("G4", "Term structure",
                      GateResult.PASS if ok else GateResult.FAIL,
                      f"M1/M2 = {term_struct:.3f}"
                      + (" (ผู้ใช้ระบุสาเหตุแล้ว)" if term_struct_explained else ""),
                      Verdict.WAIT))

    if ivp is None:
        g.append(Gate("G5", "IV floor", GateResult.UNKNOWN,
                      "ไม่มี IV Percentile", Verdict.WAIT))
    else:
        g.append(Gate("G5", "IV floor",
                      GateResult.PASS if ivp >= cfg.ivp_floor else GateResult.FAIL,
                      f"IVP {ivp:.0f} (floor {cfg.ivp_floor:.0f})", Verdict.WAIT))

    leg_vrp: Optional[float] = None
    if d.legs is None or d.side is Side.NEITHER:
        g.append(Gate("G6", "Leg VRP positive", GateResult.UNKNOWN,
                      "เลือกข้างไม่ได้", Verdict.DO_NOT_ENTER))
    else:
        leg_vrp = (d.legs.vrp_put_pts if d.side is Side.SHORT_PUT
                   else d.legs.vrp_call_pts)
        g.append(Gate("G6", "Leg VRP positive",
                      GateResult.PASS if leg_vrp > 0 else GateResult.FAIL,
                      f"{d.side.value} leg VRP = {leg_vrp:+.2f} pts",
                      Verdict.DO_NOT_ENTER))

    # --- score (คำนวณเสมอ เพื่อให้เห็นภาพ แต่ gate ตัดก่อน) -----------------
    def add(pts: int, why: str) -> None:
        d.score += pts
        d.score_detail.append(f"{pts:+d}  {why}")

    if er >= cfg.vrp_strong:
        add(3, f"VRP {er:.2f} >= {cfg.vrp_strong}")
    elif er >= cfg.vrp_good:
        add(2, f"VRP {er:.2f} >= {cfg.vrp_good}")
    elif er >= cfg.vrp_thin:
        add(1, f"VRP {er:.2f} >= {cfg.vrp_thin}")
    else:
        add(0, f"VRP {er:.2f} < {cfg.vrp_thin} — แทบไม่มี edge")

    if ivp is None:
        add(0, "IVP ไม่ทราบ")
    elif ivp >= cfg.ivp_strong:
        add(2, f"IVP {ivp:.0f} >= {cfg.ivp_strong:.0f}")
    elif ivp >= cfg.ivp_ok:
        add(1, f"IVP {ivp:.0f} >= {cfg.ivp_ok:.0f}")
    else:
        add(0, f"IVP {ivp:.0f} < {cfg.ivp_ok:.0f}")

    if leg_vrp is None or d.legs is None:
        add(0, "leg VRP ไม่ทราบ")
    else:
        other = (d.legs.vrp_call_pts if d.side is Side.SHORT_PUT
                 else d.legs.vrp_put_pts)
        if leg_vrp > 0 and other is not None and (leg_vrp - other) >= cfg.leg_edge_gap_pts:
            add(2, f"{d.side.value} leg VRP {leg_vrp:+.1f} ชนะอีกข้าง "
                   f">= {cfg.leg_edge_gap_pts} pts")
        elif leg_vrp > 0:
            add(1, f"{d.side.value} leg VRP {leg_vrp:+.1f} บวกแต่ไม่ชนะขาด")
        else:
            add(0, f"{d.side.value} leg VRP {leg_vrp:+.1f} ติดลบ")

    if trend_aligned is True:
        add(1, "trend สอดคล้องกับ delta ของ short side")
    elif trend_aligned is False:
        add(0, "trend สวนทางกับ short side")
        d.warnings.append(
            "Semivariance กับ trend ขัดกัน — ลด size ครึ่งหรือใช้ defined-risk ทั้งคู่. "
            "VRP เป็นค่าคาดหวังระยะยาวที่ต้องรอดหลายไม้ แต่ delta ผิดทางฆ่าตั้งแต่ไม้แรก")
    else:
        add(0, "ไม่ทราบ trend")

    if earnings_days is not None and earnings_days > dte + cfg.event_clear_buffer_days:
        add(1, f"ไม่มี event ใน DTE+{cfg.event_clear_buffer_days}")
    else:
        add(0, "event ใกล้หรือไม่ทราบ")

    if (spread_pct is not None and open_interest is not None
            and spread_pct <= cfg.spread_good_pct and open_interest >= cfg.oi_good):
        add(1, f"liquidity ดี (spread {spread_pct:.1f}%, OI {open_interest})")
    else:
        add(0, "liquidity ไม่เข้าเกณฑ์ดีหรือไม่ทราบ")

    # --- verdict: gate ชนะคะแนนเสมอ ----------------------------------------
    failed = d.failed_gates
    if failed:
        if any(gg.on_fail is Verdict.DO_NOT_ENTER for gg in failed):
            d.verdict = Verdict.DO_NOT_ENTER
        else:
            d.verdict = Verdict.WAIT
        d.reason = "Hard gate ไม่ผ่าน: " + ", ".join(
            f"{gg.code} {gg.name}" for gg in failed)
    else:
        if d.score >= cfg.score_enter:
            d.verdict = Verdict.ENTER
        elif d.score >= cfg.score_half:
            d.verdict = Verdict.HALF_SIZE
        elif d.score >= cfg.score_wait:
            d.verdict = Verdict.WAIT
        else:
            d.verdict = Verdict.DO_NOT_ENTER
        d.reason = f"Score {d.score}/{d.max_score} ผ่าน hard gate ทั้งหมดที่ตรวจได้"

    unknown = d.unknown_gates
    if unknown:
        d.caveats.append(
            "Gate ที่ตรวจไม่ได้เพราะข้อมูลขาด: "
            + ", ".join(f"{gg.code} ({gg.detail})" for gg in unknown)
            + " — verdict ยังไม่สมบูรณ์")

    n_unknown = len(unknown)
    if n_unknown == 0 and d.legs is not None and not d.legs.used_atm_fallback:
        d.confidence = "High"
    elif n_unknown <= 2:
        d.confidence = "Medium"
    else:
        d.confidence = "Low"

    if d.verdict in (Verdict.DO_NOT_ENTER, Verdict.WAIT):
        d.side = Side.NEITHER if d.verdict is Verdict.DO_NOT_ENTER else d.side

    return d
