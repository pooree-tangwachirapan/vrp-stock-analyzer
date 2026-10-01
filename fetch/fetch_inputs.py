#!/usr/bin/env python3
"""
fetch_inputs.py -- the only file in this repo that touches the network.

It fills in everything the analyzer would otherwise have to be typed by hand,
and writes a single JSON that index.html loads with its "Load inputs JSON"
button. The analyzer itself stays offline: it reads the file, it never fetches.

    python fetch/fetch_inputs.py AAPL
    python fetch/fetch_inputs.py QQQ --dte 45 --width 5 --analyze
    python fetch/fetch_inputs.py _SPX --dte 30          # index options

Sources, and why each one:

  Option chain  CBOE's public delayed-quote JSON. No key, no account. It is the
                only free source tested that returns implied vol, Greeks, open
                interest and two-sided quotes outside regular trading hours.
  Prices        Yahoo via yfinance, for daily OHLC and earnings dates. Its own
                option chain is not used: see docs/MARKET_DATA_APIS.md for the
                measurements behind that decision.

Nothing missing is ever filled in with a zero. A field that could not be
sourced is written as null, the analyzer shows N/A, and the gate that depended
on it stays UNKNOWN. That is the correct answer, not a gap to paper over.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "vrp_app_core"))

from vrp_app.config.defaults import VRPConfig          # noqa: E402
from vrp_app.core import vrp as vrpmod                 # noqa: E402

CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
UA = "Mozilla/5.0 (compatible; vrp-stock-analyzer/1.0; +https://github.com/pooree-tangwachirapan/vrp-stock-analyzer)"
OCC = re.compile(r"^([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")

# IV history needs to be deep enough to mean anything. Below these counts the
# field is reported as null rather than as a number nobody should act on.
MIN_OBS_PERCENTILE = 60
MIN_OBS_RANK = 120


class FetchError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# CBOE
# ---------------------------------------------------------------------------
def fetch_cboe(symbol: str) -> dict:
    """Stocks and ETFs use the plain symbol; index options need a leading _."""
    url = CBOE_URL.format(sym=symbol)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/json", "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 403:
            hint = "" if symbol.startswith("_") else " (index options need a leading underscore, e.g. _SPX)"
            raise FetchError(f"CBOE has no chain for {symbol!r}{hint}")
        raise FetchError(f"CBOE returned HTTP {e.code} for {symbol!r}")


def parse_occ(sym: str):
    m = OCC.match(sym)
    if not m:
        return None
    _, y, mo, da, cp, strike = m.groups()
    try:
        return dt.date(2000 + int(y), int(mo), int(da)), cp, int(strike) / 1000.0
    except ValueError:
        return None


def mid(o: dict):
    b, a = o.get("bid") or 0.0, o.get("ask") or 0.0
    return (b + a) / 2.0 if b > 0 and a > 0 else None


def interp(points, x):
    """Linear interpolation over (x, y) pairs; None when x sits outside them."""
    pts = sorted(p for p in points if p[0] is not None and p[1] is not None)
    if len(pts) < 2:
        return None
    if x <= pts[0][0] or x >= pts[-1][0]:
        return None
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        if x0 <= x <= x1:
            if x1 == x0:
                return y0
            return y0 + (x - x0) / (x1 - x0) * (y1 - y0)
    return None


def atm_iv(rows, spot):
    """Implied vol at the money, interpolated across the strike smile."""
    pts = [(k, o.get("iv")) for k, o in rows if o.get("iv")]
    v = interp(pts, spot)
    if v is not None:
        return v
    usable = [(k, o.get("iv")) for k, o in rows if o.get("iv")]
    return min(usable, key=lambda p: abs(p[0] - spot))[1] if usable else None


def iv_at_delta(rows, target):
    """Implied vol where |delta| equals target, interpolated along the smile."""
    pts = [(abs(o["delta"]), o["iv"]) for _, o in rows
           if o.get("delta") and o.get("iv")]
    return interp(pts, target)


def contract_at_delta(rows, target):
    cands = [(abs(abs(o["delta"]) - target), k, o) for k, o in rows if o.get("delta")]
    if not cands:
        return None, None
    _, k, o = min(cands, key=lambda c: c[0])
    return k, o


def tradeable_contract(rows, target, dmin, dmax, oi_min):
    """Pick the strike to actually trade, not merely the closest delta.

    The calibration layer hands over a delta BAND, so every strike inside it is
    a legitimate short leg. Choosing purely by nearest delta lands on dead
    strikes: on SPX it picked 7420 with 7 contracts of open interest while
    7425, five thousandths of a delta away, carried 192. Gate G3 then rejected
    a trade that was perfectly available one strike over.

    So: stay inside the band, require the open interest the gate will demand
    anyway, and among those take the one closest to the target delta. That
    keeps the trade's character while making sure there is a way out of it.
    """
    in_band = [(k, o) for k, o in rows
               if o.get("delta") and dmin <= abs(o["delta"]) <= dmax]
    liquid = [(k, o) for k, o in in_band if (o.get("open_interest") or 0) >= oi_min]
    if liquid:
        k, o = min(liquid, key=lambda x: abs(abs(x[1]["delta"]) - target))
        return k, o, None
    if in_band:
        k, o = min(in_band, key=lambda x: abs(abs(x[1]["delta"]) - target))
        return k, o, (f"No strike between delta {dmin:.2f} and {dmax:.2f} carries {oi_min} open "
                      "contracts, so the nearest delta was used and gate G3 will say so.")
    k, o = contract_at_delta(rows, target)
    return k, o, (f"No strike fell between delta {dmin:.2f} and {dmax:.2f}; the nearest delta "
                  "outside the band was used.")


def is_monthly(d: dt.date) -> bool:
    """Third Friday, the expiry that carries the deepest open interest."""
    return d.weekday() == 4 and 15 <= d.day <= 21


# ---------------------------------------------------------------------------
# Yahoo
# ---------------------------------------------------------------------------
def yahoo_symbol(symbol: str) -> str:
    return "^" + symbol[1:] if symbol.startswith("_") else symbol


def fetch_prices(symbol: str, period: str):
    import yfinance as yf
    t = yf.Ticker(yahoo_symbol(symbol))
    # auto_adjust back-adjusts all four prices together, so a split does not
    # fabricate an overnight gap that Yang-Zhang would read as real risk.
    h = t.history(period=period, auto_adjust=True)
    if h is None or len(h) == 0:
        raise FetchError(f"Yahoo returned no price history for {yahoo_symbol(symbol)!r}")
    return t, h


def earnings_info(ticker, hist, notes):
    """Days until the next report, and the size of past reaction-day moves."""
    days_to, past = None, None
    today = dt.date.today()
    try:
        ed = ticker.get_earnings_dates(limit=24)
    except Exception as e:                                    # noqa: BLE001
        notes.append(f"Earnings dates unavailable: {type(e).__name__}")
        return None, None
    if ed is None or len(ed) == 0:
        notes.append("Yahoo returned no earnings dates.")
        return None, None

    dates = sorted({d.date() for d in ed.index})
    future = [d for d in dates if d > today]
    if future:
        days_to = (future[0] - today).days

    closes = hist["Close"]
    idx = [d.date() for d in closes.index]
    moves = []
    for d in [d for d in dates if d <= today]:
        # The reaction lands on the first session strictly after the report,
        # which covers both before-open and after-close releases.
        nxt = next((i for i, day in enumerate(idx) if day > d), None)
        if nxt is None or nxt == 0:
            continue
        prev, cur = float(closes.iloc[nxt - 1]), float(closes.iloc[nxt])
        if prev > 0:
            moves.append(round(abs(cur / prev - 1.0) * 100.0, 2))
    if moves:
        past = moves[-8:]
    else:
        notes.append("No past earnings reactions fell inside the price history.")
    return days_to, past


# ---------------------------------------------------------------------------
# IV history log -- the only way to get IV Rank and IV Percentile for free
# ---------------------------------------------------------------------------
def update_iv_history(path: str, today: dt.date, iv30_pct, notes):
    rows = []
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    rows.append((dt.date.fromisoformat(r["date"]), float(r["atm_iv_pct"])))
                except (ValueError, KeyError):
                    continue
    if iv30_pct is not None:
        rows = [r for r in rows if r[0] != today] + [(today, iv30_pct)]
        rows.sort()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["date", "atm_iv_pct"])
            for d, v in rows:
                w.writerow([d.isoformat(), f"{v:.4f}"])

    series = [v for _, v in rows]
    ivp = ivr = lo = hi = None
    if iv30_pct is None:
        return None, None, None, None, len(series)
    if len(series) >= MIN_OBS_PERCENTILE:
        ivp = vrpmod.iv_percentile(iv30_pct, series)
    else:
        notes.append(
            f"IV Percentile needs {MIN_OBS_PERCENTILE} daily observations and the log holds "
            f"{len(series)}. Run this daily and it will fill in; until then the field is null "
            "and gate G5 stays UNKNOWN.")
    if len(series) >= MIN_OBS_RANK:
        lo, hi = min(series), max(series)
        if hi > lo:
            ivr = vrpmod.iv_rank(iv30_pct, lo, hi)
        else:
            lo = hi = None
    elif len(series) >= MIN_OBS_PERCENTILE:
        notes.append(f"IV Rank needs {MIN_OBS_RANK} observations; the log holds {len(series)}.")
    return ivr, ivp, lo, hi, len(series)


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------
def build(symbol: str, target_dte: int, width: float, delta_target: float,
          period: str, hist_dir: str) -> dict:
    cfg = VRPConfig()
    notes: list[str] = []
    today = dt.date.today()

    raw = fetch_cboe(symbol)
    data = raw.get("data") or {}
    spot = data.get("current_price") or data.get("close")
    if not spot:
        raise FetchError("CBOE returned no price for the underlying")

    by_exp: dict[dt.date, dict[str, list]] = {}
    for o in data.get("options", []):
        p = parse_occ(o.get("option", ""))
        if p:
            by_exp.setdefault(p[0], {"C": [], "P": []})[p[1]].append((p[2], o))
    exps = sorted(e for e in by_exp if (e - today).days > 0)
    if not exps:
        raise FetchError("CBOE returned no expiries in the future")

    # Choosing the expiry is the same problem as choosing the strike. The
    # calibration layer hands over a DTE BAND, so anything inside it is
    # allowed, and inside the band an expiry nobody has traded is not really an
    # option. SPX lists Monday weeklies that can sit three days nearer the
    # target with zero open interest on every single strike, while the monthly
    # just beyond carries thousands. So: stay in the band, keep only expiries
    # where some strike in the delta band holds the open interest gate G3 will
    # demand, then take the one closest to the requested DTE.
    def band_depth(e):
        best = 0
        for _, o in by_exp[e]["P"]:
            if o.get("delta") and cfg.delta_min <= abs(o["delta"]) <= cfg.delta_max:
                best = max(best, o.get("open_interest") or 0)
        return best

    in_band = [e for e in exps if cfg.dte_min <= (e - today).days <= cfg.dte_max]
    tradeable = [e for e in in_band if band_depth(e) >= cfg.oi_min]
    nearest = min(exps, key=lambda e: abs((e - today).days - target_dte))
    if tradeable:
        target = min(tradeable, key=lambda e: abs((e - today).days - target_dte))
        if target != nearest:
            notes.append(
                f"Skipped the {(nearest - today).days}-day expiry {nearest.isoformat()}: no strike in "
                f"the delta band holds {cfg.oi_min} open contracts. Using {target.isoformat()} instead, "
                f"which does.")
    else:
        target = nearest
        notes.append(
            f"No expiry between {cfg.dte_min} and {cfg.dte_max} days has a strike in the delta band "
            f"with {cfg.oi_min} open contracts, so the closest to {target_dte} days was used and gate "
            "G3 will report what it finds.")
    dte = (target - today).days
    puts, calls = by_exp[target]["P"], by_exp[target]["C"]

    # IV30: interpolate the at-the-money vol in time across the two expiries
    # that bracket 30 days, rather than taking whichever expiry happens to be
    # nearest and calling it a 30-day number.
    iv30 = None
    before = [e for e in exps if (e - today).days <= 30]
    after = [e for e in exps if (e - today).days >= 30]
    if before and after:
        a, b = before[-1], after[0]
        iva, ivb = atm_iv(by_exp[a]["P"], spot), atm_iv(by_exp[b]["P"], spot)
        da, db = (a - today).days, (b - today).days
        if iva and ivb:
            iv30 = iva if da == db else iva + (30 - da) / (db - da) * (ivb - iva)
            notes.append(f"IV30 interpolated between {da}d ({iva*100:.2f}%) and {db}d ({ivb*100:.2f}%).")
    if iv30 is None:
        iv30 = atm_iv(puts, spot)
        if iv30:
            notes.append(f"IV30 taken from the {dte}-day expiry; no pair of expiries brackets 30 days.")

    iv_put25 = iv_at_delta(puts, delta_target)
    iv_call25 = iv_at_delta(calls, delta_target)
    if iv_put25 is None or iv_call25 is None:
        notes.append(
            f"Could not interpolate a delta-{delta_target:.2f} implied vol on both sides; "
            "the analyzer will fall back to ATM and lower its confidence.")

    monthlies = [e for e in exps if is_monthly(e)]
    iv_m1 = iv_m2 = None
    if len(monthlies) >= 2:
        iv_m1 = atm_iv(by_exp[monthlies[0]]["P"], spot)
        iv_m2 = atm_iv(by_exp[monthlies[1]]["P"], spot)
    else:
        notes.append("Fewer than two monthly expiries available, so no term structure.")

    # the short leg, and the liquidity that goes with it
    k_short, o_short, pick_note = tradeable_contract(
        puts, delta_target, cfg.delta_min, cfg.delta_max, cfg.oi_min)
    if pick_note:
        notes.append(pick_note)
    elif o_short and o_short.get("delta"):
        notes.append(
            f"Short strike {k_short:g} at delta {abs(o_short['delta']):.3f}, chosen as the closest "
            f"to {delta_target:.2f} among strikes inside the {cfg.delta_min:.2f}-{cfg.delta_max:.2f} "
            f"band that carry at least {cfg.oi_min} open contracts.")
    spread_pct = open_interest = short_delta = None
    if o_short:
        short_delta = abs(o_short.get("delta")) if o_short.get("delta") else None
        oi = o_short.get("open_interest")
        open_interest = int(oi) if oi else None
        m = mid(o_short)
        if m and m > 0:
            spread_pct = (o_short["ask"] - o_short["bid"]) / m * 100.0
        else:
            notes.append("The delta-25 put had no two-sided quote, so the spread is null. "
                         "Outside US market hours this is normal; re-run during the session.")

    # a defined-risk put spread at that strike, priced off the mids
    credit = spread_width = None
    if k_short is not None:
        longs = [(k, o) for k, o in puts if k < k_short]
        if longs:
            k_long, o_long = min(longs, key=lambda x: abs((k_short - x[0]) - width))
            ms, ml = mid(o_short), mid(o_long)
            if ms is not None and ml is not None and ms > ml:
                spread_width = round(k_short - k_long, 2)
                credit = round(ms - ml, 2)
            else:
                notes.append("No two-sided quotes on both legs, so the credit is null.")

    ticker, hist = fetch_prices(symbol, period)
    earnings_days, past_moves = earnings_info(ticker, hist, notes)

    # The at-the-money straddle prices the expected move over the whole life of
    # the contract. It is only an EARNINGS jump when the report actually falls
    # inside that life. Emitting it otherwise would have the analyzer strip a
    # jump that is not in the IV, which understates the diffusive vol and makes
    # a fair trade look bad. So the straddle is always reported, and
    # impliedMovePct is filled only when the event is genuinely in the window.
    straddle_move_pct = None
    if puts and calls:
        _, po = min(puts, key=lambda x: abs(x[0] - spot))
        _, co = min(calls, key=lambda x: abs(x[0] - spot))
        mp, mc = mid(po), mid(co)
        if mp is not None and mc is not None:
            straddle_move_pct = (mp + mc) * cfg.straddle_to_move / spot * 100.0

    implied_move_pct = None
    if straddle_move_pct is None:
        notes.append("No two-sided ATM straddle quote, so no expected move could be priced.")
    elif earnings_days is None:
        notes.append(
            f"The ATM straddle prices a {straddle_move_pct:.2f}% expected move, but the earnings "
            "date is unknown, so it is not being treated as an event jump. Set it by hand if a "
            "report falls inside this expiry.")
    elif earnings_days <= dte:
        implied_move_pct = straddle_move_pct
        notes.append(
            f"Earnings land in {earnings_days} days, inside this {dte}-day expiry, so the ATM "
            f"straddle ({mp + mc:.2f}) is read as the implied event move using the straddle "
            f"factor {cfg.straddle_to_move} from the calibration layer.")
    else:
        notes.append(
            f"Earnings are {earnings_days} days out and this expiry runs {dte} days, so no event "
            f"sits inside the contract. The {straddle_move_pct:.2f}% the straddle prices is ordinary "
            "diffusive move, not a jump, and stripping it out would understate the diffusive vol. "
            "impliedMovePct is left null on purpose.")

    closes = [float(c) for c in hist["Close"]]
    trend = None
    if len(closes) >= 50:
        sma50 = sum(closes[-50:]) / 50.0
        trend = "up" if closes[-1] > sma50 else "down"
        notes.append(f"Price is {(closes[-1]/sma50 - 1)*100:+.1f}% against its 50-day average "
                     f"({trend}trend).")

    iv30_pct = iv30 * 100.0 if iv30 else None
    hist_path = os.path.join(hist_dir, f"{symbol}.csv")
    iv_rank, iv_pct, iv_lo, iv_hi, n_obs = update_iv_history(hist_path, today, iv30_pct, notes)

    ohlc = []
    for ts, row in hist.iterrows():
        ohlc.append({
            "date": ts.date().isoformat(),
            "open": round(float(row["Open"]), 4),
            "high": round(float(row["High"]), 4),
            "low": round(float(row["Low"]), 4),
            "close": round(float(row["Close"]), 4),
        })

    def pct(x):
        return round(x * 100.0, 4) if x is not None else None

    return {
        "schema": "vrp-inputs/1",
        "generatedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "symbol": symbol,
        "sources": {
            "options": "CBOE delayed quotes (cdn.cboe.com)",
            "optionsTimestamp": raw.get("timestamp"),
            "prices": f"Yahoo Finance via yfinance ({yahoo_symbol(symbol)}, split-adjusted)",
            "ivHistory": f"{os.path.relpath(hist_path, REPO)} ({n_obs} observations)",
        },
        "expiry": target.isoformat(),
        "notes": notes,
        "inputs": {
            "iv30": pct(iv30),
            "ivPut25": pct(iv_put25),
            "ivCall25": pct(iv_call25),
            "ivM1": pct(iv_m1),
            "ivM2": pct(iv_m2),
            "ivRank": round(iv_rank, 1) if iv_rank is not None else None,
            "iv1yLow": round(iv_lo, 2) if iv_lo is not None else None,
            "iv1yHigh": round(iv_hi, 2) if iv_hi is not None else None,
            "ivPercentile": round(iv_pct, 1) if iv_pct is not None else None,
            "spot": round(float(spot), 2),
            "dte": dte,
            "spreadPct": round(spread_pct, 2) if spread_pct is not None else None,
            "openInterest": open_interest,
            "shortDelta": round(short_delta, 4) if short_delta is not None else None,
            "shortStrike": k_short,
            "earningsDays": earnings_days,
            "impliedMovePct": round(implied_move_pct, 2) if implied_move_pct is not None else None,
            "straddleMovePct": round(straddle_move_pct, 2) if straddle_move_pct is not None else None,
            "historicalMoves": past_moves,
            "spreadWidth": spread_width,
            "credit": credit,
            "trendDirection": trend,
        },
        "ohlc": ohlc,
    }


# ---------------------------------------------------------------------------
# optional headless run of the analyzer
# ---------------------------------------------------------------------------
def analyze(payload: dict) -> None:
    import numpy as np
    from vrp_app.core import estimators as est
    from vrp_app.engine.decide import decide

    cfg = VRPConfig()
    i = payload["inputs"]
    close = np.array([r["close"] for r in payload["ohlc"]], dtype=float)
    if i["iv30"] is None:
        print("\nCannot analyze: no IV30 was available.")
        return

    fc = vrpmod.forecast_rv(close, cfg.har_weights)
    v = vrpmod.compute_vrp(i["iv30"] / 100.0, fc.value)
    semi = est.semivariance(close, min(60, len(close) - 1))
    event = None
    if i["impliedMovePct"] is not None:
        try:
            event = vrpmod.decompose_event(i["iv30"] / 100.0, i["dte"], i["impliedMovePct"] / 100.0)
        except Exception as e:                                # noqa: BLE001
            print(f"  event decomposition refused: {e}")

    def dec(x):
        return x / 100.0 if x is not None else None

    d = decide(
        cfg=cfg, vrp=v, dte=i["dte"], event=event, semi=semi,
        iv_put25=dec(i["ivPut25"]), iv_call25=dec(i["ivCall25"]),
        ivp=i["ivPercentile"],
        term_struct=(i["ivM1"] / i["ivM2"]) if i["ivM1"] and i["ivM2"] else None,
        earnings_days=i["earningsDays"], spread_pct=i["spreadPct"],
        open_interest=i["openInterest"],
    )
    print(f"\n  E[RV30]          {fc.value*100:.2f}%")
    print(f"  VRP ratio (raw)  {v.ratio:.3f}")
    print(f"  effective ratio  {d.effective_ratio:.3f} ({d.effective_ratio_source})")
    print(f"  VERDICT          {d.verdict.value}  {d.side.value}  score {d.score}/{d.max_score}  "
          f"confidence {d.confidence}")
    for g in d.gates:
        print(f"    {g.code} {g.result.value:<8} {g.detail}")
    for w in d.warnings:
        print(f"  ! {w}")


def main() -> int:
    # The Python reference carries Thai gate text. Without this the Windows
    # console renders it as mojibake under the legacy codepage.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbol", help="AAPL, QQQ, or _SPX for index options")
    ap.add_argument("--dte", type=int, default=38, help="target days to expiry (default 38)")
    ap.add_argument("--width", type=float, default=5.0, help="spread width to price (default 5)")
    ap.add_argument("--delta", type=float, default=0.25, help="target short delta (default 0.25)")
    ap.add_argument("--period", default="2y", help="price history to pull (default 2y)")
    ap.add_argument("--out", default=os.path.join(HERE, "out"))
    ap.add_argument("--history-dir", default=os.path.join(HERE, "iv_history"))
    ap.add_argument("--analyze", action="store_true", help="also run the engine and print a verdict")
    a = ap.parse_args()

    sym = a.symbol.upper()
    try:
        payload = build(sym, a.dte, a.width, a.delta, a.period, a.history_dir)
    except FetchError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, f"{sym}_vrp.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    i = payload["inputs"]
    filled = sum(1 for v in i.values() if v is not None)
    print(f"{sym}  expiry {payload['expiry']} ({i['dte']} DTE)  "
          f"options stamped {payload['sources']['optionsTimestamp']}")
    print(f"  {len(payload['ohlc'])} price rows, {filled}/{len(i)} fields filled")
    for k in ("iv30", "ivPut25", "ivCall25", "ivM1", "ivM2", "ivRank", "ivPercentile",
              "iv1yLow", "iv1yHigh",
              "spot", "spreadPct", "openInterest", "shortDelta", "shortStrike",
              "earningsDays", "impliedMovePct", "straddleMovePct", "spreadWidth", "credit",
              "trendDirection"):
        v = i[k]
        print(f"  {k:<16} {'null' if v is None else v}")
    if i["historicalMoves"]:
        print(f"  {'historicalMoves':<16} {i['historicalMoves']}")
    for n in payload["notes"]:
        print(f"  - {n}")
    print(f"\nwrote {os.path.relpath(path, REPO)}")
    print("Load it with the \"Load inputs JSON\" button in index.html.")

    if a.analyze:
        analyze(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
