# VRP Stock Analyzer

A Variance Risk Premium calculator for deciding whether to sell options on a single stock.

**[Open the tool](https://pooree-tangwachirapan.github.io/vrp-stock-analyzer/)** — one HTML file, no build step, no network calls. It works just as well saved to disk and opened with `file://`.

---

## What it does

It answers one question: *is the implied volatility in this option rich enough, relative to what the stock is actually likely to do, to be worth selling?*

Feed it a daily OHLC history and the option quotes you are looking at. It gives back a verdict (ENTER / HALF SIZE / WAIT / DO NOT ENTER), the hard gates that produced it, a score breakdown, and a trade plan when the trade clears.

The number it leads with is **variance**, not volatility. The P&L of a delta-hedged short option is

```
½ ∫ Γ · S² · (σ_implied² − σ_realized²) dt
```

so what you collect is the difference in *variance*, weighted by dollar gamma. `IV − RV` appears only because it reads more easily.

## The failure mode it exists to catch

A screener that compares raw IV against realized vol will light up on any stock with earnings inside the expiry. That premium is not a diffusive edge — it is the price of a jump, and a delta hedge cannot harvest it gradually.

Given an implied earnings move, the tool strips the jump out:

```
T       = DTE / 365
IV_diff = sqrt( (IV_total²·T − J²) / T )
```

and judges the trade on `IV_diff / E[RV]` instead. When the raw ratio is above 1 but the clean one is below it, the verdict flips to DO NOT ENTER and says why in as many words. If `J` is larger than the total variance can support, it raises an error rather than clamping to zero, because that means one of the two inputs is wrong.

## The one architectural rule

```
CORE    deterministic maths. Published closed forms only.
        No threshold, no weight, no scale that someone chose.
CONFIG  every tunable number, in one place, each tagged with its provenance.
ENGINE  the only layer allowed to combine the two.
VIEW    renders. Computes nothing.
```

If `1.15` or `0.50` appears in the core, that is a bug, and a self-test fails on it. This matters because it keeps the question "is this formula right?" separate from "is this threshold tuned well?" — the first has an answer, the second does not.

## Using it

**The quick way.** Run the fetcher once and load the file it writes:

```bash
python fetch/fetch_inputs.py AAPL --analyze
```

That fills seventeen of the nineteen fields from CBOE's public delayed-quote
feed and Yahoo's price history, including the delta-25 implied vols, the term
structure, open interest, the spread, and the size of past earnings moves. Open
the analyzer, press **Load inputs JSON**, and pick `fetch/out/AAPL_vrp.json`.
The page itself still makes no network call: fetching happens in the script,
analysis happens offline. See [fetch/README.md](fetch/README.md).

The two it cannot fill are IV Rank and IV Percentile, because no free source
publishes a history of implied vol. The fetcher logs today's at-the-money IV on
every run and they appear once enough days have accumulated; type them in from
your broker in the meantime.

**Price history by hand.** CSV with `Date, Open, High, Low, Close`. Column names are case insensitive, `Adj Close` is accepted in place of `Close`, and rows are sorted by date for you. At least 21 rows are required; below 130 the HAR blend renormalizes its weights and says so. With only a `Close` column you still get close-to-close RV, but no Parkinson, Garman-Klass, Yang-Zhang or gap ratio.

`sample/sample_ohlc.csv` is 260 rows of **synthetic** data for trying the tool out. It is not a real stock. The "Load synthetic demo" button generates a similar series in the page.

**Option inputs.** IV30 and DTE are the minimum. Everything else widens what can be checked, and anything left blank shows as `N/A` — never as zero. Gates that cannot be checked come back `UNKNOWN`, which is not a pass, and the verdict carries a caveat saying the picture is incomplete.

## How a verdict is reached

Six hard gates run first. One failure ends it, whatever the score says.

| Gate | Fails when |
|---|---|
| G1 | the effective VRP ratio is below 1.00 |
| G2 | earnings fall inside the DTE and you have not said you are trading the event |
| G3 | the spread is too wide or open interest too thin |
| G4 | the term structure is in backwardation with no stated cause |
| G5 | the IV percentile is below the floor |
| G6 | the chosen leg's own VRP is not positive |

Only then does the 0–10 score decide between ENTER, HALF SIZE and WAIT. Every threshold in that table lives in the Calibration panel and can be changed from the UI.

## Things it refuses to do

- No network calls of any kind. Nothing you type leaves the page.
- Nothing missing is filled in with a zero or an average.
- Negative results are never clamped to look better.
- A win rate is never shown without the expectancy beside it, and `PoP` is never shown without `P_touch ≈ 2·Δ` — a stop-on-touch rule fires roughly twice as often as the expiry probability suggests.
- A naked short call is never proposed, at any score. Covered or defined-risk only.
- A cash-secured put is not proposed unless you confirm the cash is there.

## Tests

**In the browser.** The Self-tests panel runs 46 checks in the page: the semivariance identity across 1000 paths, known-vol recovery, estimator ordering under a gap regime, the event-decomposition algebra, gate short-circuiting, graceful degradation when inputs are missing, and a scan of the core's own source for calibration constants that should not be there.

One of them is a cross-language fixture: a price path built from a closed form with no randomness, whose expected answers were produced by the Python implementation in this repo. If the JavaScript ever drifts from the reference in any formula, that test fails.

**The Python reference.** `vrp_app_core/` holds the implementation the browser version was ported from, and is the source of truth for the mathematics.

```bash
cd vrp_app_core
pip install -r requirements.txt
python -m pytest -v
```

## Documents

- [VRP_CodeMode_Prompt.md](VRP_CodeMode_Prompt.md) — the original specification (Thai)
- [docs/MARKET_DATA_APIS.md](docs/MARKET_DATA_APIS.md) — what each option-data source
  actually returns, measured: CBOE, Webull, Yahoo, and why the first one won
- [fetch/README.md](fetch/README.md) — the fetcher

## Where the numbers come from

Published, and used as given: Parkinson (1980), Garman-Klass (1980), Yang-Zhang (2000), Rogers-Satchell, and 252 trading days a year.

Chosen by hand, and worth arguing with: the HAR weights (Corsi 2009 uses 1/5/22 days; the 0.50/0.30/0.20 split here is not canonical), every VRP threshold, the √2 semivol scale, the delta and DTE windows, and the risk budgets. All of them are tagged `heuristic` in the Calibration panel, with a note on where they came from.

Single-stock VRP is much thinner than index VRP. Driessen, Maenhout and Vilkov (2009, *Journal of Finance*) showed the index premium comes from a correlation risk premium rather than a volatility premium, and that single-stock options are priced close to fair. The thresholds here are set higher than index work would need, and should still be treated as a starting point.

## Disclaimer

Statistical analysis, not investment advice. VRP is a long-run expectation: a single trade can still lose in full. Selling premium means winning often and losing big, so position sizing matters more than the accuracy of the VRP estimate.

Variance that arrives while the price sits near the strike is the variance that hurts. A quiet month with one burst at the strike loses money even when the average VRP was positive, and no number in this tool captures that path dependency.
