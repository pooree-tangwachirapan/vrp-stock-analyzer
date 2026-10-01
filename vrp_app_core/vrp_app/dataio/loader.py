"""
CSV loader + validation (§5) — fail fast, ห้ามเดาค่าที่ขาด

ชื่อแพ็กเกจเป็น `dataio` ไม่ใช่ `io` โดยตั้งใจ: `io` เป็นชื่อโมดูล stdlib
การตั้งชื่อซ้ำทำให้เกิดปัญหา import ที่ debug ยากเมื่อรันจากคนละ working dir
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

REQUIRED = ("open", "high", "low", "close")
MIN_ROWS_HARD = 21          # ต่ำกว่านี้คำนวณ RV20 ไม่ได้
MIN_ROWS_FULL = 130         # ต่ำกว่านี้ HAR blend ต้อง renormalize
IV_MIN_PCT, IV_MAX_PCT = 5.0, 300.0


class DataError(ValueError):
    pass


@dataclass
class LoadResult:
    df: pd.DataFrame
    rows: int
    has_ohlc: bool
    warnings: list[str] = field(default_factory=list)

    @property
    def close(self) -> np.ndarray:
        return self.df["close"].to_numpy(dtype=float)

    def col(self, name: str) -> np.ndarray:
        return self.df[name].to_numpy(dtype=float)


def load_ohlc(source) -> LoadResult:
    """
    รับ path, file-like หรือ DataFrame
    ชื่อคอลัมน์ case-insensitive, รับ 'Adj Close', จัดเรียงตามวันที่ให้เอง
    """
    df = source.copy() if isinstance(source, pd.DataFrame) else pd.read_csv(source)
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]

    if "close" not in df.columns and "adj_close" in df.columns:
        df["close"] = df["adj_close"]

    if "close" not in df.columns:
        raise DataError("ไม่พบคอลัมน์ Close (หรือ Adj Close)")

    warnings: list[str] = []

    date_col = next((c for c in ("date", "datetime", "time") if c in df.columns), None)
    if date_col:
        df["date"] = pd.to_datetime(df[date_col], errors="coerce")
        bad_dates = int(df["date"].isna().sum())
        if bad_dates:
            warnings.append(f"ตัด {bad_dates} แถวที่วันที่อ่านไม่ได้")
            df = df.dropna(subset=["date"])
        df = df.sort_values("date").reset_index(drop=True)
    else:
        warnings.append("ไม่มีคอลัมน์วันที่ — ถือว่าข้อมูลเรียงจากเก่าไปใหม่แล้ว")

    for c in REQUIRED:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    has_ohlc = all(c in df.columns for c in REQUIRED)
    if not has_ohlc:
        missing = [c for c in REQUIRED if c not in df.columns]
        warnings.append(
            f"ขาดคอลัมน์ {missing} — คำนวณได้เฉพาะ Close-to-Close "
            "(ไม่มี Parkinson/Garman-Klass/Yang-Zhang และไม่มี gap_ratio)")

    check_cols = list(REQUIRED) if has_ohlc else ["close"]
    before = len(df)
    df = df.dropna(subset=check_cols)
    if len(df) < before:
        warnings.append(f"ตัด {before - len(df)} แถวที่มีค่าว่าง")

    if (df[check_cols] <= 0).to_numpy().any():
        raise DataError("พบราคา <= 0 — ข้อมูลเสียหาย")

    if has_ohlc:
        bad = df[(df["high"] < df["low"])
                 | (df["high"] < df["close"]) | (df["high"] < df["open"])
                 | (df["low"] > df["close"]) | (df["low"] > df["open"])]
        if len(bad):
            idx = ", ".join(str(i) for i in bad.index[:10])
            raise DataError(
                f"พบ {len(bad)} แถวที่ OHLC ไม่สมเหตุสมผล "
                f"(High ต้อง >= ทุกค่า, Low ต้อง <= ทุกค่า) ที่แถว: {idx}")

    df = df.reset_index(drop=True)
    rows = len(df)

    if rows < MIN_ROWS_HARD:
        raise DataError(
            f"ข้อมูลเหลือ {rows} แถว — ต้องการอย่างน้อย {MIN_ROWS_HARD} เพื่อคำนวณ RV20")
    if rows < MIN_ROWS_FULL:
        warnings.append(
            f"มี {rows} แถว (<{MIN_ROWS_FULL}) — HAR blend จะ renormalize น้ำหนัก "
            "และ E[RV] จะอิงหน้าต่างสั้นมากกว่าที่ตั้งใจ")

    return LoadResult(df=df, rows=rows, has_ohlc=has_ohlc, warnings=warnings)


def normalize_iv(value: float | None, label: str = "IV") -> tuple[float | None, list[str]]:
    """
    รับ IV เป็น % (52.4) หรือ decimal (0.524) คืนเป็น decimal

    ไม่เดาเงียบ ๆ: ทุกการตีความจะคืน warning กลับไปให้ UI แสดง
    ค่าที่ขาดคืน None ไม่ใช่ 0
    """
    if value is None:
        return None, []
    v = float(value)
    notes: list[str] = []
    if v <= 0:
        raise DataError(f"{label} ต้อง > 0 (ได้ {v})")
    if v < 1.0:
        notes.append(f"{label}={v} ตีความเป็น decimal ({v:.1%})")
        dec = v
    else:
        dec = v / 100.0
    pct = dec * 100.0
    if pct < IV_MIN_PCT or pct > IV_MAX_PCT:
        notes.append(
            f"{label} = {pct:.1f}% อยู่นอกช่วงปกติ {IV_MIN_PCT:.0f}-{IV_MAX_PCT:.0f}% "
            "— ตรวจหน่วยอีกครั้ง")
    return dec, notes
