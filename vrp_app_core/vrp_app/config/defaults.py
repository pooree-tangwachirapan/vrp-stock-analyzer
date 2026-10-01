"""
CALIBRATION LAYER — ค่าที่เราเลือกเองทั้งหมดอยู่ที่นี่ที่เดียว

ทุกฟิลด์ต้องระบุว่าเป็น heuristic หรือ derived และอ้างที่มา
ถ้าค่าเหล่านี้โผล่ไปอยู่ใน core/ = ผิด spec (tests/test_core.py::test_config_isolation ตรวจ)

คำเตือนสำคัญที่ต้องส่งต่อไปถึง UI:
VRP ระดับ index (SPX/SPY) ใหญ่และเสถียร แต่ VRP ระดับ "หุ้นเดี่ยว" เล็กกว่ามาก
Driessen, Maenhout & Vilkov (2009, Journal of Finance) แสดงว่าส่วนต่างของ index
มาจาก correlation risk premium ไม่ใช่ volatility premium — option หุ้นเดี่ยว
ราคาค่อนข้างแฟร์ ดังนั้น threshold ด้านล่างจึงตั้งสูงกว่าที่ใช้กับ index
และยังควรถือว่าเป็นจุดเริ่มต้นสำหรับการจูน ไม่ใช่ค่าที่พิสูจน์แล้ว
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Literal

Provenance = Literal["heuristic", "derived", "convention", "published"]


@dataclass(frozen=True)
class FieldNote:
    provenance: Provenance
    note: str


@dataclass
class VRPConfig:
    # ---- E[RV] forecast ----------------------------------------------------
    har_weights: dict[int, float] = field(
        default_factory=lambda: {20: 0.50, 60: 0.30, 120: 0.20})

    # ---- VRP thresholds ----------------------------------------------------
    vrp_strong: float = 1.25
    vrp_good: float = 1.15
    vrp_thin: float = 1.05

    # ---- side selection ----------------------------------------------------
    # sqrt(2): ถ้า return สมมาตร semivol * sqrt(2) = vol เต็ม พอดี
    # ใช้เพื่อให้เทียบ IV (ซึ่งเป็น vol เต็ม) กับ semivol ได้ — ไม่ใช่ผล no-arbitrage
    semivol_scale: float = 1.4142135623730951
    leg_edge_gap_pts: float = 2.0      # ชนะอีกข้างกี่ vol points ถึงนับว่าชนะขาด

    # ---- event -------------------------------------------------------------
    straddle_to_move: float = 0.85     # straddle price -> expected |move|
    earnings_edge_ratio: float = 1.25  # implied/median actual ที่ถือว่ามี edge

    # ---- gates -------------------------------------------------------------
    ivp_floor: float = 20.0
    ts_backwardation: float = 1.05
    spread_max_pct: float = 10.0
    oi_min: int = 100
    gap_ratio_warn: float = 1.25

    # ---- liquidity bonus ---------------------------------------------------
    spread_good_pct: float = 5.0
    oi_good: int = 500

    # ---- scoring -----------------------------------------------------------
    ivp_strong: float = 50.0
    ivp_ok: float = 30.0
    event_clear_buffer_days: int = 5
    score_enter: int = 8
    score_half: int = 6
    score_wait: int = 4

    # ---- position ----------------------------------------------------------
    dte_min: int = 30
    dte_max: int = 45
    delta_min: float = 0.15
    delta_max: float = 0.30
    tp_pct: float = 0.50
    sl_credit_multiple: float = 2.0
    time_exit_dte: int = 21
    risk_full: float = 0.015
    risk_half: float = 0.0075
    min_credit_to_width: float = 0.25

    def validate(self) -> list[str]:
        """คืนรายการปัญหา — ว่างแปลว่า config สมเหตุสมผล"""
        issues: list[str] = []
        if not (self.vrp_thin < self.vrp_good < self.vrp_strong):
            issues.append("vrp thresholds ต้องเรียง thin < good < strong")
        if self.vrp_thin < 1.0:
            issues.append(
                "vrp_thin < 1.00 — แปลว่ายอมให้คะแนนบวกกับ VRP ติดลบ ซึ่งขัดกับ G1")
        if not (self.score_wait < self.score_half < self.score_enter):
            issues.append("score bands ต้องเรียง wait < half < enter")
        if not (0 < self.delta_min < self.delta_max < 1):
            issues.append("delta range ไม่ถูกต้อง")
        if not (0 < self.risk_half <= self.risk_full < 1):
            issues.append("risk_half ต้อง <= risk_full และทั้งคู่อยู่ 0..1")
        if self.dte_min > self.dte_max:
            issues.append("dte_min > dte_max")
        if abs(sum(self.har_weights.values())) <= 0:
            issues.append("har_weights รวมแล้ว <= 0")
        if self.ivp_floor < 0 or self.ivp_floor > 100:
            issues.append("ivp_floor ต้องอยู่ 0..100")
        return issues

    def to_dict(self) -> dict:
        return asdict(self)


# ที่มาของแต่ละค่า — UI ใช้ทำ tooltip
PROVENANCE: dict[str, FieldNote] = {
    "har_weights": FieldNote(
        "heuristic",
        "HAR-inspired (Corsi 2009 ใช้ 1/5/22 วัน). น้ำหนัก 0.50/0.30/0.20 "
        "เป็นค่าที่เลือกเอง ไม่ใช่ค่า canonical — จูนได้"),
    "vrp_strong": FieldNote("heuristic", "practitioner threshold, ไม่มีงานวิจัยรองรับตัวเลขนี้"),
    "vrp_good": FieldNote("heuristic", "practitioner threshold"),
    "vrp_thin": FieldNote("heuristic", "practitioner threshold"),
    "semivol_scale": FieldNote(
        "heuristic",
        "sqrt(2) ให้ semivol เทียบกับ vol เต็มได้เมื่อ return สมมาตร. "
        "put IV ที่สูงกว่าส่วนหนึ่งเป็น crash risk premium ที่มีเหตุผล "
        "ไม่ใช่ mispricing ทั้งหมด"),
    "straddle_to_move": FieldNote(
        "heuristic", "ATM straddle -> E|move|; 0.85 เป็นค่าที่ใช้กันทั่วไป"),
    "ivp_floor": FieldNote("heuristic", "vol floor — ต่ำกว่านี้ premium ไม่คุ้ม slippage"),
    "ts_backwardation": FieldNote("heuristic", "เกณฑ์ backwardation ที่ถือว่าผิดปกติ"),
    "gap_ratio_warn": FieldNote(
        "heuristic", "RV_yz/RV_cc ที่ถือว่า overnight risk สูงพอจะลด size"),
    "tp_pct": FieldNote("heuristic", "ปิดที่ 50% ของ credit — practitioner convention"),
    "time_exit_dte": FieldNote(
        "heuristic", "21 DTE — gamma risk เร่งขึ้นหลังจากนี้; เป็น convention ไม่ใช่ผลวิจัย"),
    "delta_min": FieldNote("heuristic", "ช่วง delta ที่ใช้กันทั่วไปสำหรับ short premium"),
    "delta_max": FieldNote("heuristic", "ช่วง delta ที่ใช้กันทั่วไปสำหรับ short premium"),
    "risk_full": FieldNote("heuristic", "1.5% ต่อไม้ — risk budget ที่เลือกเอง"),
    "risk_half": FieldNote("heuristic", "0.75% ต่อไม้"),
    "earnings_edge_ratio": FieldNote(
        "heuristic", "implied/median actual ที่ถือว่ามี edge พอจะเทรด event"),
}

DISCLAIMER = (
    "วิเคราะห์เชิงสถิติ ไม่ใช่คำแนะนำการลงทุน — VRP เป็นค่าคาดหวังระยะยาว "
    "ไม้เดี่ยวยังแพ้ได้เต็มที่ Short premium คือชนะบ่อยแพ้หนัก "
    "sizing สำคัญกว่าความแม่นของ VRP"
)
