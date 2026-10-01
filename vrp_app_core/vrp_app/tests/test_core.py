"""
Test suite §7 — ทุกข้อต้องผ่านก่อนไปขั้น UI

รัน: python -m pytest vrp_app/tests/test_core.py -v
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from vrp_app.config.defaults import VRPConfig
from vrp_app.core import estimators as est
from vrp_app.core import position as pos
from vrp_app.core import vrp as vrpmod
from vrp_app.dataio.loader import DataError, load_ohlc, normalize_iv
from vrp_app.engine.decide import GateResult, Side, Verdict, decide

CORE_DIR = Path(__file__).resolve().parent.parent / "core"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def gbm_closes(sigma: float, n: int, seed: int, s0: float = 100.0,
               mu: float = 0.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    dt = 1.0 / est.ANNUALIZATION_DAYS
    r = rng.normal((mu - 0.5 * sigma ** 2) * dt, sigma * math.sqrt(dt), n)
    return s0 * np.exp(np.cumsum(r))


def ohlc_from_closes(close: np.ndarray, seed: int, gap_sigma: float,
                     intraday_sigma: float):
    """สร้าง OHLC ที่ควบคุมสัดส่วน overnight gap vs intraday range ได้"""
    rng = np.random.default_rng(seed)
    n = close.size
    op = close * np.exp(rng.normal(0, gap_sigma, n))
    hi = np.maximum(op, close) * np.exp(np.abs(rng.normal(0, intraday_sigma, n)))
    lo = np.minimum(op, close) * np.exp(-np.abs(rng.normal(0, intraday_sigma, n)))
    return op, hi, lo


def make_vrp(iv: float, rv: float):
    return vrpmod.compute_vrp(iv, rv)


# ---------------------------------------------------------------------------
# TEST 1 — semivariance identity (hard assertion, 1000 ชุด)
# ---------------------------------------------------------------------------
def test_semivariance_identity_holds_across_many_paths():
    for seed in range(1000):
        close = gbm_closes(sigma=0.35, n=80, seed=seed)
        s = est.semivariance(close, window=60)
        lhs = s.down ** 2 + s.up ** 2
        rhs = s.total ** 2
        assert math.isclose(lhs, rhs, rel_tol=1e-9), f"seed={seed}"
        assert s.identity_residual <= 1e-9
        assert s.n_down + s.n_up + s.n_zero == 60


def test_semivariance_matches_rv_cc_on_same_window():
    close = gbm_closes(sigma=0.28, n=300, seed=7)
    s = est.semivariance(close, window=60)
    assert math.isclose(s.total, est.rv_close_to_close(close, 60), rel_tol=1e-12)


def test_semivariance_handles_zero_returns():
    """r_i == 0 ต้องไม่เข้าทั้งสองฝั่งและไม่ทำ identity พัง"""
    close = np.array([100.0] * 10 + [101.0, 101.0, 100.0, 100.0] * 8, dtype=float)
    s = est.semivariance(close, window=30)
    assert s.n_zero > 0
    assert math.isclose(s.down ** 2 + s.up ** 2, s.total ** 2, rel_tol=1e-12)


# ---------------------------------------------------------------------------
# TEST 2 — known-vol recovery
# ---------------------------------------------------------------------------
def test_rv_cc_recovers_known_sigma():
    close = gbm_closes(sigma=0.30, n=5000, seed=42)
    rv = est.rv_close_to_close(close, window=5000 - 1)
    assert 0.28 <= rv <= 0.32, rv


@pytest.mark.parametrize("sigma", [0.15, 0.30, 0.60, 1.20])
def test_rv_cc_unbiased_across_vol_levels(sigma):
    close = gbm_closes(sigma=sigma, n=6000, seed=11)
    rv = est.rv_close_to_close(close, window=5999)
    assert abs(rv - sigma) / sigma < 0.05, (sigma, rv)


def test_all_estimators_agree_when_no_gaps():
    """
    เมื่อไม่มี overnight gap ตัวประมาณทุกตัวควรให้ค่าใกล้เคียงกัน
    (open = close ของวันก่อน) — ถ้าไม่ใกล้ แปลว่าสูตรตัวใดตัวหนึ่งผิด
    """
    close = gbm_closes(sigma=0.30, n=1200, seed=3)
    op = np.empty_like(close)
    op[0] = close[0]
    op[1:] = close[:-1]                       # ไม่มี gap
    rng = np.random.default_rng(3)
    noise = np.abs(rng.normal(0, 0.004, close.size))
    hi = np.maximum(op, close) * np.exp(noise)
    lo = np.minimum(op, close) * np.exp(-noise)

    w = 1000
    cc = est.rv_close_to_close(close, w)
    pk = est.rv_parkinson(hi, lo, w)
    gk = est.rv_garman_klass(op, hi, lo, close, w)
    yz = est.rv_yang_zhang(op, hi, lo, close, w)
    for name, v in (("parkinson", pk), ("gk", gk), ("yz", yz)):
        assert abs(v - cc) / cc < 0.25, (name, v, cc)


# ---------------------------------------------------------------------------
# TEST 3 — estimator ordering under gap regime
# ---------------------------------------------------------------------------
def test_parkinson_underestimates_when_gaps_dominate():
    """
    Parkinson/GK ใช้เฉพาะ range ภายในวัน จึงตาบอดต่อ overnight gap โดยโครงสร้าง
    เมื่อ variance ส่วนใหญ่มาจาก gap ทั้งคู่ต้องต่ำกว่า CC และ YZ อย่างมีนัย

    หมายเหตุ: spec เดิมเขียน RV_yz > RV_cc > RV_park แต่ YZ > CC ไม่ใช่
    การรับประกันทางทฤษฎี (YZ เป็น minimum-variance estimator ของ vol ตัวเดียวกัน
    ไม่ใช่ตัวที่สูงกว่าเสมอ) จึงทดสอบเฉพาะส่วนที่เป็นจริงเชิงโครงสร้าง
    """
    close = gbm_closes(sigma=0.50, n=400, seed=5)
    op, hi, lo = ohlc_from_closes(close, seed=5, gap_sigma=0.040,
                                  intraday_sigma=0.002)
    w = 250
    cc = est.rv_close_to_close(close, w)
    pk = est.rv_parkinson(hi, lo, w)
    gk = est.rv_garman_klass(op, hi, lo, close, w)
    yz = est.rv_yang_zhang(op, hi, lo, close, w)

    assert pk < cc, (pk, cc)
    assert gk < cc, (gk, cc)
    assert pk < yz and gk < yz
    assert est.gap_ratio(yz, cc) > 1.0


def test_gap_ratio_near_one_without_gaps():
    close = gbm_closes(sigma=0.30, n=400, seed=9)
    op = np.empty_like(close)
    op[0] = close[0]
    op[1:] = close[:-1]
    rng = np.random.default_rng(9)
    noise = np.abs(rng.normal(0, 0.004, close.size))
    hi = np.maximum(op, close) * np.exp(noise)
    lo = np.minimum(op, close) * np.exp(-noise)
    w = 250
    ratio = est.gap_ratio(est.rv_yang_zhang(op, hi, lo, close, w),
                          est.rv_close_to_close(close, w))
    assert 0.7 < ratio < 1.3, ratio


# ---------------------------------------------------------------------------
# TEST 4 — event decomposition
# ---------------------------------------------------------------------------
def test_iv_diffusive_always_below_total_when_jump_positive():
    for move in (0.02, 0.05, 0.09, 0.14):
        d = vrpmod.decompose_event(iv_total=0.60, dte=38, implied_move=move)
        assert d.iv_diffusive < d.iv_total
        assert d.diffusive_variance > 0
        # ตรวจ algebra ย้อนกลับ
        t = 38 / 365.0
        assert math.isclose(
            d.iv_diffusive ** 2 * t + move ** 2, 0.60 ** 2 * t, rel_tol=1e-12)


def test_zero_jump_leaves_iv_unchanged():
    d = vrpmod.decompose_event(iv_total=0.60, dte=38, implied_move=0.0)
    assert math.isclose(d.iv_diffusive, 0.60, rel_tol=1e-12)


def test_oversized_jump_raises_not_clamps():
    """J ใหญ่กว่า total variance = input ขัดกันเอง ต้อง raise ไม่ใช่คืน 0"""
    with pytest.raises(vrpmod.VRPError, match="Event decomposition"):
        vrpmod.decompose_event(iv_total=0.30, dte=7, implied_move=0.25)


def test_earnings_vrp_uses_median_not_mean():
    """median ทนต่อควอเตอร์เดียวที่เหวี่ยงผิดปกติ — ต้องไม่ใช่ mean"""
    moves = np.array([5.0, 6.0, 5.5, 6.5, 5.0, 6.0, 5.5, 40.0])
    v, med = vrpmod.earnings_vrp(10.0, moves)
    assert math.isclose(med, 5.75)
    assert math.isclose(v, 4.25)


# ---------------------------------------------------------------------------
# TEST 5 — gate short-circuits score
# ---------------------------------------------------------------------------
def _full_score_kwargs(cfg, iv, rv, **over):
    close = gbm_closes(sigma=rv, n=200, seed=1)
    semi = est.semivariance(close, window=60)
    base = dict(
        cfg=cfg, vrp=make_vrp(iv, rv), dte=38, semi=semi,
        iv_put25=iv * 1.10, iv_call25=iv * 0.90,
        ivp=70.0, term_struct=0.96, earnings_days=90,
        spread_pct=2.0, open_interest=3000, trend_aligned=True,
    )
    base.update(over)
    return base


def test_negative_vrp_forces_do_not_enter_regardless_of_score():
    cfg = VRPConfig()
    d = decide(**_full_score_kwargs(cfg, iv=0.30, rv=0.40))
    assert d.verdict is Verdict.DO_NOT_ENTER
    assert any(g.code == "G1" and g.result is GateResult.FAIL for g in d.gates)


def test_event_in_window_forces_wait():
    cfg = VRPConfig()
    d = decide(**_full_score_kwargs(cfg, iv=0.60, rv=0.30, earnings_days=10))
    assert d.verdict is Verdict.WAIT
    assert any(g.code == "G2" and g.result is GateResult.FAIL for g in d.gates)


def test_bad_liquidity_forces_do_not_enter():
    cfg = VRPConfig()
    d = decide(**_full_score_kwargs(cfg, iv=0.60, rv=0.30,
                                    spread_pct=25.0, open_interest=4))
    assert d.verdict is Verdict.DO_NOT_ENTER


def test_clean_setup_reaches_enter():
    cfg = VRPConfig()
    d = decide(**_full_score_kwargs(cfg, iv=0.60, rv=0.30))
    assert d.verdict is Verdict.ENTER
    assert d.score >= cfg.score_enter
    assert d.confidence == "High"
    assert not d.failed_gates and not d.unknown_gates


# ---------------------------------------------------------------------------
# TEST 6 — Clean VRP overrides raw (failure mode หลัก)
# ---------------------------------------------------------------------------
def test_clean_vrp_overrides_positive_raw_vrp():
    """
    raw VRP บวก แต่ premium ทั้งก้อนมาจาก event
    -> effective_ratio ต้องใช้ clean, verdict ต้องเป็น DO NOT ENTER, ต้องมี warning
    """
    cfg = VRPConfig()
    iv, rv = 0.58, 0.48                       # raw ratio ~1.21
    ev = vrpmod.decompose_event(iv_total=iv, dte=38, implied_move=0.115)
    d = decide(**_full_score_kwargs(cfg, iv=iv, rv=rv, event=ev,
                                    earnings_days=90, trading_the_event=True))

    assert d.effective_ratio_source == "clean"
    assert make_vrp(iv, rv).ratio > 1.0
    assert d.effective_ratio < 1.0
    assert d.verdict is Verdict.DO_NOT_ENTER
    assert any("Clean VRP ติดลบ" in w for w in d.warnings)


def test_clean_vrp_still_positive_keeps_trade_alive():
    cfg = VRPConfig()
    iv, rv = 0.70, 0.30
    ev = vrpmod.decompose_event(iv_total=iv, dte=38, implied_move=0.05)
    d = decide(**_full_score_kwargs(cfg, iv=iv, rv=rv, event=ev))
    assert d.effective_ratio > 1.0
    assert d.verdict in (Verdict.ENTER, Verdict.HALF_SIZE)
    assert not any("Clean VRP ติดลบ" in w for w in d.warnings)


# ---------------------------------------------------------------------------
# TEST 7 — missing data degrades gracefully, ไม่ crash ไม่เดา
# ---------------------------------------------------------------------------
def test_missing_delta25_falls_back_to_atm_with_warning():
    cfg = VRPConfig()
    kw = _full_score_kwargs(cfg, iv=0.60, rv=0.30)
    kw["iv_put25"] = None
    kw["iv_call25"] = None
    d = decide(**kw)
    assert d.legs is not None and d.legs.used_atm_fallback
    assert any("delta-25" in w for w in d.warnings)
    assert d.confidence in ("Medium", "Low")


def test_unknown_gates_are_not_pass():
    cfg = VRPConfig()
    d = decide(cfg=cfg, vrp=make_vrp(0.60, 0.30), dte=38)
    unknown = {g.code for g in d.unknown_gates}
    assert {"G2", "G3", "G4", "G5", "G6"} <= unknown
    assert d.caveats
    assert d.confidence == "Low"


def test_no_silent_zero_defaults():
    """ค่าที่ขาดต้องเป็น None ไม่ใช่ 0 — 0 จะถูกตีความว่า 'วัดได้และเป็นศูนย์'"""
    cfg = VRPConfig()
    d = decide(cfg=cfg, vrp=make_vrp(0.60, 0.30), dte=38)
    assert d.legs is None
    v, notes = normalize_iv(None)
    assert v is None and notes == []


# ---------------------------------------------------------------------------
# TEST 8 — config isolation (บังคับ architecture ไม่ใช่แค่หวัง)
# ---------------------------------------------------------------------------
FORBIDDEN_IN_CORE = [
    r"1\.25", r"1\.15", r"1\.05",     # VRP thresholds
    r"0\.85",                          # straddle -> move
    r"1\.41421",                       # semivol scale
    r"0\.015", r"0\.0075",             # risk budgets
]


@pytest.mark.parametrize("path", sorted(CORE_DIR.glob("*.py")))
def test_core_has_no_calibration_constants(path):
    """
    core/ ต้องมีเฉพาะค่าคงที่จากสูตรที่ตีพิมพ์แล้ว
    (252, 4ln2, 0.5/2ln2-1 ของ GK, 0.34/1.34 ของ YZ)
    ค่าที่เราเลือกเองต้องอยู่ใน config/ ทั้งหมด
    """
    src = path.read_text(encoding="utf-8")
    code_lines = []
    in_docstring = False
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.count('"""') == 1:
            in_docstring = not in_docstring
            continue
        if in_docstring or stripped.startswith("#"):
            continue
        code_lines.append(line.split("#")[0])
    code = "\n".join(code_lines)

    for pattern in FORBIDDEN_IN_CORE:
        hit = re.search(pattern, code)
        assert hit is None, (
            f"{path.name} มี calibration constant /{pattern}/ "
            f"ที่บรรทัด: {hit.group(0)!r} — ย้ายไป config/")


def test_core_does_not_import_config():
    """
    ตรวจ import จริงด้วย AST ไม่ใช่ grep ข้อความ —
    docstring ที่เขียนว่า "ห้าม import config" จะทำให้ grep ให้ false positive
    """
    import ast

    for path in CORE_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "config" not in alias.name.split("."), \
                        f"{path.name} import {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert "config" not in mod.split("."), f"{path.name} from {mod}"


def test_forecast_rv_requires_explicit_weights():
    """ถ้า forecast_rv มี default weights แปลว่า calibration หลุดเข้า core"""
    import inspect
    sig = inspect.signature(vrpmod.forecast_rv)
    assert sig.parameters["weights"].default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# เพิ่มเติม — VRP math, position math, loader
# ---------------------------------------------------------------------------
def test_vrp_variance_is_the_headline_number():
    v = make_vrp(0.60, 0.40)
    assert math.isclose(v.variance, 0.60 ** 2 - 0.40 ** 2, rel_tol=1e-12)
    assert math.isclose(v.ratio, 1.5, rel_tol=1e-12)
    assert math.isclose(v.vol_points, 20.0, rel_tol=1e-12)


def test_vrp_selfcheck_tight_when_iv_near_rv():
    v = make_vrp(0.305, 0.300)
    assert v.selfcheck_residual < 0.05


def test_vrp_rejects_nonpositive_inputs():
    for iv, rv in ((0.0, 0.3), (0.3, 0.0), (-0.1, 0.3)):
        with pytest.raises(vrpmod.VRPError):
            make_vrp(iv, rv)


def test_har_renormalizes_when_window_missing():
    close = gbm_closes(sigma=0.3, n=70, seed=2)
    f = vrpmod.forecast_rv(close, {20: 0.50, 60: 0.30, 120: 0.20})
    assert f.windows_used == (20, 60)
    assert f.renormalized
    assert math.isclose(sum(f.weights_effective.values()), 1.0, rel_tol=1e-12)
    assert math.isclose(f.weights_effective[20], 0.5 / 0.8, rel_tol=1e-12)


def test_har_blend_lies_between_components():
    close = gbm_closes(sigma=0.3, n=400, seed=4)
    f = vrpmod.forecast_rv(close, {20: 0.50, 60: 0.30, 120: 0.20})
    assert min(f.components.values()) <= f.value <= max(f.components.values())


def test_touch_probability_is_double_expiry_probability():
    p = pos.probabilities(0.20)
    assert math.isclose(p.pop_expiry, 0.80, rel_tol=1e-12)
    assert math.isclose(p.p_touch, 0.40, rel_tol=1e-12)


def test_high_win_rate_can_still_be_negative_expectancy():
    """short put Δ0.20 ชนะ 80% แต่ถ้าแพ้หนักพอ E ติดลบ — ต้องจับได้"""
    p = pos.probabilities(0.20)
    e = pos.expectancy(p.pop_expiry, avg_win=100.0, avg_loss=600.0)
    assert e.pop == 0.80
    assert not e.is_positive


def test_sizing_returns_zero_when_trade_too_big():
    s = pos.position_size(portfolio=10_000, risk_pct=0.015,
                          max_loss_per_contract=5_000)
    assert s.contracts == 0


def test_credit_spread_rejects_credit_above_width():
    with pytest.raises(pos.PositionError):
        pos.credit_spread_max_loss(width=5.0, credit=5.5)


def test_expected_move_scales_with_sqrt_time():
    a = pos.expected_move(100.0, 0.40, 30)
    b = pos.expected_move(100.0, 0.40, 120)
    assert math.isclose(b / a, 2.0, rel_tol=1e-12)


def _frame(n=150, seed=1):
    close = gbm_closes(0.35, n, seed)
    op, hi, lo = ohlc_from_closes(close, seed, 0.01, 0.01)
    return pd.DataFrame({
        "Date": pd.bdate_range("2026-01-01", periods=n).strftime("%Y-%m-%d"),
        "Open": op, "High": hi, "Low": lo, "Close": close})


def test_loader_accepts_clean_frame():
    r = load_ohlc(_frame())
    assert r.has_ohlc and r.rows == 150 and not r.warnings


def test_loader_rejects_impossible_ohlc():
    df = _frame()
    df.loc[10, "High"] = df.loc[10, "Low"] - 1.0
    with pytest.raises(DataError, match="OHLC ไม่สมเหตุสมผล"):
        load_ohlc(df)


def test_loader_rejects_too_few_rows():
    with pytest.raises(DataError, match="อย่างน้อย"):
        load_ohlc(_frame(n=15))


def test_loader_warns_on_short_history():
    r = load_ohlc(_frame(n=100))
    assert any("renormalize" in w for w in r.warnings)


def test_normalize_iv_handles_both_units():
    dec, notes = normalize_iv(52.4)
    assert math.isclose(dec, 0.524)
    dec2, notes2 = normalize_iv(0.524)
    assert math.isclose(dec2, 0.524) and notes2


def test_normalize_iv_flags_out_of_range():
    _, notes = normalize_iv(450.0)
    assert any("นอกช่วงปกติ" in n for n in notes)


def test_config_validate_catches_bad_thresholds():
    cfg = VRPConfig(vrp_thin=1.30, vrp_good=1.15, vrp_strong=1.25)
    assert cfg.validate()
    assert not VRPConfig().validate()


def test_config_rejects_threshold_below_parity():
    cfg = VRPConfig(vrp_thin=0.95, vrp_good=1.15, vrp_strong=1.25)
    assert any("ขัดกับ G1" in i for i in cfg.validate())
