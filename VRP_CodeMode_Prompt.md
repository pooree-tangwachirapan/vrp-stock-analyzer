# PROMPT สำหรับ AI Code Mode — VRP Stock Analyzer

> วิธีใช้: copy ทั้งไฟล์นี้วางใน Claude Code / Cursor / Code Mode
> เลือก Target ที่ §0 แล้วลบอีกอันทิ้ง

---

## §0 TARGET (เลือกอันเดียว)

- [ ] **A. Streamlit app** — multi-file Python, upload CSV, interactive
- [ ] **B. Single-file HTML** — self-contained, no build step, no network, เปิดจาก file:// ได้

---

## §1 ROLE & NON-NEGOTIABLES

สร้างเครื่องมือคำนวณ Variance Risk Premium (VRP) สำหรับตัดสินใจขาย option รายตัวหุ้น

**หลักสถาปัตยกรรมข้อเดียวที่ห้ามละเมิด: แยก deterministic math ออกจาก calibration**

```
core/      ← สูตรปิด ตายตัว ทดสอบได้ ห้าม import config ห้ามมี magic number
config/    ← threshold, weights, heuristics ทั้งหมดอยู่ที่นี่ที่เดียว
ui/        ← แสดงผลอย่างเดียว ห้ามคำนวณ
```

ถ้าเขียนเลข 1.15 หรือ 0.50 ลงใน `core/` = ผิด spec

**กฎเพิ่ม:**
1. ทุกค่าที่แสดงต้อง trace กลับไปหา input ได้ — ห้ามมีตัวเลขที่ "ประมาณเอา"
2. ขาด input → แสดง `N/A` + บอกว่าขาดอะไร **ห้าม default เป็น 0 หรือเดาค่า**
3. ทุก output ที่เป็น heuristic ต้องติด flag `is_heuristic=True` และ UI ต้องแสดงสัญลักษณ์ต่าง
4. ห้ามเรียก network ใด ๆ — input มาจาก CSV upload / manual entry เท่านั้น

---

## §2 DETERMINISTIC CORE (สูตรตายตัว — implement ตรงตามนี้)

`A = 252` (trading days/year), `rᵢ = ln(Cᵢ/Cᵢ₋₁)`

### 2.1 RV Estimators

```
RV_cc    = √( (A/n) · Σ rᵢ² )

RV_park  = √( A/(4·n·ln2) · Σ (ln(Hᵢ/Lᵢ))² )

RV_gk    = √( (A/n) · Σ [ 0.5·(ln(Hᵢ/Lᵢ))² − (2ln2−1)·(ln(Cᵢ/Oᵢ))² ] )

RV_yz:
  oᵢ = ln(Oᵢ/Cᵢ₋₁)   cᵢ = ln(Cᵢ/Oᵢ)   uᵢ = ln(Hᵢ/Oᵢ)   dᵢ = ln(Lᵢ/Oᵢ)
  σ²_o  = Σ(oᵢ−ō)²/(n−1)
  σ²_c  = Σ(cᵢ−c̄)²/(n−1)
  σ²_rs = Σ[ uᵢ(uᵢ−cᵢ) + dᵢ(dᵢ−cᵢ) ]/n          (Rogers-Satchell)
  k     = 0.34 / (1.34 + (n+1)/(n−1))
  RV_yz = √( A·(σ²_o + k·σ²_c + (1−k)·σ²_rs) )
```

ห้าม demean ใน RV_cc สำหรับหน้าต่างสั้น (drift << noise, การ demean เพิ่ม estimation error)

### 2.2 Semivariance

```
RV_down² = (A/n) · Σ rᵢ²·1{rᵢ<0}
RV_up²   = (A/n) · Σ rᵢ²·1{rᵢ>0}
```

**HARD ASSERTION (ไม่ใช่ warning):** `|RV_down² + RV_up² − RV_cc²| / RV_cc² < 1e-9`
ถ้าไม่ผ่าน → raise exception ทันที อย่าคืนค่าออกไป

### 2.3 VRP

```
VRP_vol   = IV30 − E[RV30]
VRP_var   = IV30² − E[RV30]²
VRP_ratio = IV30 / E[RV30]
```
Sanity: `VRP_var / (2·IV30) ≈ VRP_vol` เมื่อ IV≈RV (first-order) — ใช้เป็น self-check

### 2.4 Event decomposition (เมื่อให้ J มา)

```
T = DTE/365
J = implied_earnings_move (decimal)
IV_diff = √( (IV_total²·T − J²) / T )
```
ถ้า `IV_total²·T − J² ≤ 0` → raise error "J ใหญ่กว่า total variance, input ผิด" **ห้าม clamp เป็น 0**

### 2.5 IV metrics & position math

```
IV_Rank = (IV − IV_min_1Y)/(IV_max_1Y − IV_min_1Y) × 100
IV_Pct  = count(IV_hist < IV_today)/252 × 100
TS      = IV_M1 / IV_M2
EM_1sd  = S · IV · √(DTE/365)
PoP     ≈ 1 − |Δ_short|
P_touch ≈ 2 × |Δ_short|
E       = PoP·AvgWin − (1−PoP)·AvgLoss
```

---

## §3 CALIBRATION LAYER (ทั้งหมดไปอยู่ใน config — ผู้ใช้แก้ได้จาก UI)

```python
@dataclass
class VRPConfig:
    # E[RV] forecast — HAR-inspired (Corsi 2009), ไม่ใช่ค่า canonical
    har_weights: dict = {20: 0.50, 60: 0.30, 120: 0.20}   # renormalize ถ้าข้อมูลไม่พอ

    # VRP thresholds — practitioner heuristic
    vrp_strong: float = 1.25
    vrp_good: float   = 1.15
    vrp_thin: float   = 1.05

    # semivol -> vol normalization (heuristic, ไม่ใช่ no-arbitrage)
    semivol_scale: float = 1.4142135623730951   # √2

    # straddle -> expected |move|
    straddle_to_move: float = 0.85

    # gates
    ivp_floor: float = 20
    ts_backwardation: float = 1.05
    spread_max_pct: float = 10
    oi_min: int = 100
    gap_ratio_warn: float = 1.25    # RV_yz/RV_cc

    # scoring weights + verdict bands
    score_bands: dict = {"ENTER": 8, "HALF": 6, "WAIT": 4}

    # position
    dte_target: tuple = (30, 45)
    delta_range: tuple = (0.15, 0.30)
    tp_pct: float = 0.50
    time_exit_dte: int = 21
    risk_full: float = 0.015
    risk_half: float = 0.0075
```

ทุกฟิลด์ต้องมี tooltip ใน UI บอกว่า **"heuristic"** หรือ **"derived"** และอ้างที่มา

---

## §4 DECISION ENGINE

### Hard gates (ตกข้อเดียว = จบ ไม่ต้องคิดคะแนน)

| Gate | เงื่อนไขตก | ผล |
|---|---|---|
| G1 | `effective_ratio < 1.00` | DO NOT ENTER |
| G2 | `earnings_days ≤ DTE` และไม่ได้ตั้งใจเทรด event | WAIT |
| G3 | `spread_pct > 10` หรือ `OI < 100` | DO NOT ENTER |
| G4 | `TS > 1.05` โดยไม่ระบุสาเหตุ | WAIT |
| G5 | `IVP < 20` | WAIT |
| G6 | leg VRP ของข้างที่เลือก ≤ 0 | DO NOT ENTER (ข้างนั้น) |

**สำคัญ — `effective_ratio`:**
```
ถ้าถอด event ได้ (มี J และ DTE) → ใช้ clean_vrp_ratio = IV_diff / E[RV]
ถ้าไม่ได้                        → ใช้ raw vrp_ratio
```
และถ้า `raw_ratio ≥ 1.0 > clean_ratio` → ต้องขึ้น warning เด่นชัด:
*"VRP ดิบบวกแต่ Clean VRP ติดลบ — premium ทั้งก้อนมาจาก event ไม่ใช่ diffusive edge"*
นี่คือ failure mode หลักของ screener ทั่วไป ห้ามพลาด

Gate ที่ข้อมูลไม่พอตรวจ → `None` (ไม่ใช่ True) และ verdict ต้องติด caveat ว่ายังไม่สมบูรณ์

### Side selection

```
VRP_put  = IV(Δ25P) − RV_down · semivol_scale
VRP_call = IV(Δ25C) − RV_up   · semivol_scale
skew_gap = (IV_Δ25P/IV_Δ25C) / (RV_down/RV_up)
```
ไม่มี IV Δ25 → fallback ATM ทั้งสองข้าง + ลด confidence + แสดงเตือน

**Conflict rule:** ถ้า semivariance ชี้ข้างหนึ่งแต่ trend/assignment ชี้อีกข้าง → บังคับลด size ครึ่ง
เหตุผล: VRP เป็นค่าคาดหวังระยะยาวต้องรอดหลายไม้ แต่ delta ผิดทางฆ่าตั้งแต่ไม้แรก

### Score (0–10)

```
effective_ratio ≥1.25→+3 | ≥1.15→+2 | ≥1.05→+1 | else 0
IVP ≥50→+2 | ≥30→+1 | else 0
leg VRP ข้างที่เลือก: ชนะอีกข้าง ≥2pts→+2 | บวกเฉย ๆ→+1 | ติดลบ→0
trend สอดคล้องกับ delta → +1
ไม่มี event ใน DTE+5 → +1
liquidity เต็ม (spread≤5%, OI≥500) → +1
```
Verdict: ≥8 ENTER | ≥6 HALF SIZE | ≥4 WAIT | ≤3 DO NOT ENTER

---

## §5 DATA CONTRACT

**CSV:** `Date,Open,High,Low,Close` (รับ `Adj Close`, ชื่อคอลัมน์ case-insensitive, auto-sort)

**Validation ก่อนคำนวณ — fail fast:**
| เช็ค | ถ้าไม่ผ่าน |
|---|---|
| rows ≥ 21 | error |
| High ≥ Low, Low ≤ Close ≤ High ทุกแถว | error + ชี้แถวที่เสีย |
| ไม่มี Close ≤ 0 หรือ NaN | error |
| IV อยู่ช่วง 5–300 | ถ้า <5 เตือนว่าน่าจะเป็น decimal ไม่ใช่ % |
| rows ≥ 130 | warning: HAR renormalized |

**Manual inputs:** IV30, IV_Δ25P, IV_Δ25C, IV_M1, IV_M2, IVR, IVP, spot, DTE,
earnings_days, implied_move, hist_moves[8], spread_pct, OI, side, trend_aligned

---

## §6A TARGET A — STREAMLIT

```
vrp_app/
├── app.py                  # UI เท่านั้น
├── core/
│   ├── estimators.py       # §2.1, §2.2  (pure, no config import)
│   ├── vrp.py              # §2.3, §2.4
│   └── position.py         # §2.5
├── config/defaults.py      # §3 dataclass
├── engine/decide.py        # §4 (import ทั้ง core และ config)
├── io/loader.py            # §5
└── tests/test_core.py
```

Stack: `streamlit pandas numpy plotly` เท่านั้น ห้าม yfinance หรือ network call

**Layout:**
- Sidebar: upload CSV, manual IV inputs, config expander (ทุกค่าใน §3 แก้ได้ + reset)
- Main: Verdict banner (สี) → VRP core metrics → RV estimators table (+ gap_ratio flag)
  → Side selection (put vs call เทียบกัน) → Event panel (raw vs clean เด่นชัด)
  → Gates checklist → Score breakdown → Trade plan (เฉพาะ ENTER/HALF)
  → Data & Confidence
- ปุ่ม export JSON + export HTML report

**Charts (plotly):** IV vs RV ทุก estimator / semivariance แยกสองข้าง / score waterfall

---

## §6B TARGET B — SINGLE-FILE HTML

ไฟล์เดียว `vrp.html` เปิดด้วย `file://` ได้ ไม่มี build step ไม่มี CDN ไม่มี fetch

Vanilla JS + inline CSS เท่านั้น (ถ้าต้องการ chart ให้วาดด้วย SVG/canvas เอง)

**Layout:** กรอก IV manual + ปุ่ม paste/upload CSV (ใช้ `FileReader`) → ปุ่ม Calculate
→ panel ผลลัพธ์เหมือน §6A

**เหมือนกันทุกประการ:** โครงสร้าง pure-function / config object / decision engine
แยกเป็น 3 ก้อนใน `<script>` ชัดเจน — `CORE`, `CONFIG`, `ENGINE` ห้ามปนกัน

Export: ปุ่ม copy JSON + ปุ่ม print-to-PDF (`@media print` stylesheet)

**Dark mode:** ประกาศสีเป็น CSS custom properties บน `:root`, override ใต้
`@media (prefers-color-scheme: dark)` และกำหนด background ให้ `body` ชัดเจน

---

## §7 TESTS (ต้องมี ไม่ใช่ optional)

1. **Semivariance identity** — random walk 1000 ชุด, ทุกชุดต้องผ่าน `RV_down²+RV_up²=RV_cc²`
2. **Known-vol** — สร้าง GBM σ=0.30, n=5000 → `RV_cc` ต้องอยู่ 0.28–0.32
3. **Estimator ordering** — ข้อมูลที่มี gap แรง → `RV_yz > RV_cc > RV_park` (Parkinson ตาบอด overnight)
4. **Event decomp** — `IV_diff < IV_total` เสมอเมื่อ J>0; `J` ใหญ่เกิน → raise ไม่ใช่คืน 0
5. **Gate short-circuit** — `ratio=0.9` ต้องได้ DO NOT ENTER ไม่ว่าคะแนนอื่นจะเต็มแค่ไหน
6. **Clean-VRP override** — raw 1.20 / clean 0.85 → ต้อง DO NOT ENTER + ขึ้น warning
7. **Missing data** — ไม่มี IV_Δ25 → fallback ATM + confidence ลด + ไม่ crash
8. **Config isolation** — grep `core/` ต้องไม่เจอเลข 1.15, 1.25, 0.50, 0.85

---

## §8 ห้ามทำ (anti-requirements)

- ❌ เรียก network / yfinance / API ใด ๆ
- ❌ default ค่าที่ขาดเป็น 0 หรือค่าเฉลี่ย — ต้องเป็น `None` + แสดง N/A
- ❌ clamp ผลลัพธ์ติดลบให้เป็น 0 เพื่อให้ "ดูดี"
- ❌ ฝัง threshold ใน core
- ❌ แสดง win rate โดยไม่แสดง expectancy คู่กัน
- ❌ แสดง `PoP` โดยไม่แสดง `P_touch ≈ 2×Δ` (stop-on-touch ตัด win rate ลงราวครึ่ง)
- ❌ แนะนำ naked short call ทุกกรณี — ต้องเป็น covered หรือ defined-risk spread
- ❌ เสนอ cash-secured put เมื่อผู้ใช้ระบุว่าเงินสดไม่พอรับ 100 หุ้น

---

## §9 บริบทที่ต้องเข้าใจ (ใส่ไว้ใน docstring ของ engine)

**ทำไมเป็น variance ไม่ใช่ vol:** P&L ของ short option ที่ delta-hedge คือ
`½∫Γ·S²·(σ_imp² − σ_real²)dt` — เก็บส่วนต่าง **variance** ถ่วงด้วย dollar gamma
→ ตัวเลขหลักคือ `IV²−RV²` ส่วน `IV−RV` ใช้สื่อสารอย่างเดียว
นัยสำคัญ: variance ที่เกิดตอนราคาใกล้ strike สำคัญที่สุด — RV ต่ำทั้งเดือนแต่พุ่งตอนแตะ strike
= ขาดทุนได้แม้ VRP เฉลี่ยบวก (path dependency)

**ทำไมหุ้นเดี่ยวต้องใช้ threshold สูงกว่า index:** Driessen, Maenhout & Vilkov (2009, JF)
แสดงว่า premium ของ index option มาจาก **correlation risk premium** ไม่ใช่ vol premium
ส่วน option หุ้นเดี่ยวราคาค่อนข้างแฟร์ → edge บางกว่ามาก ต้องการ VRP_ratio สูงกว่าถึงจะคุ้ม tail

**ทำไมต้อง forecast ไม่ใช่ trailing:** VRP ที่ถูกต้องคือ IV วันนี้ vs RV **อนาคต**
เรามีแค่ RV อดีต → ใช้ blend หลายหน้าต่างลด overreaction ต่อ spike เดี่ยว (vol clustering, Corsi 2009)

**Disclaimer ท้าย UI:** *"วิเคราะห์เชิงสถิติ ไม่ใช่คำแนะนำการลงทุน — VRP เป็นค่าคาดหวังระยะยาว
ไม้เดี่ยวยังแพ้ได้เต็มที่ Short premium คือชนะบ่อยแพ้หนัก sizing สำคัญกว่าความแม่นของ VRP"*

---

## §10 ลำดับการ build

1. `core/` + tests ให้ผ่านทั้งหมดก่อน — **ยังไม่ต้องแตะ UI**
2. `config/` + `engine/` + gate tests
3. `io/` validation
4. UI
5. Export

ส่งงานทีละขั้น ให้ผม review ก่อนขั้นถัดไป
