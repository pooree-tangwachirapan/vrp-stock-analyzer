# fetch_inputs.py

The only file in this repo that touches the network. It fills in the fields the
analyzer would otherwise need typed by hand, and writes one JSON that
`index.html` loads with its **Load inputs JSON** button.

The split is deliberate: fetching happens here, analysis happens in a page that
never makes a request. Nothing you enter in the analyzer leaves your machine.

```bash
pip install -r ../vrp_app_core/requirements.txt yfinance
python fetch_inputs.py AAPL
python fetch_inputs.py QQQ --dte 45 --analyze
python fetch_inputs.py _SPX --dte 30          # index options need the underscore
```

Then open the analyzer and load `out/AAPL_vrp.json`.

## What it fills in

Seventeen of the nineteen fields, with no market-hours restriction.

| From CBOE's delayed-quote feed | From Yahoo |
|---|---|
| IV30, interpolated in time between the two expiries that bracket 30 days | daily OHLC, split-adjusted |
| IV at delta 25 on both sides, interpolated along the smile | days until the next earnings report |
| IV of the first two monthly expiries, for the term structure | the size of the last eight earnings-day moves |
| the delta-25 strike, its delta, its bid-ask spread and open interest | the 50-day trend direction |
| a defined-risk spread at that strike, with its width and credit | |
| the at-the-money straddle | |

`--analyze` also runs the Python engine and prints the verdict, so you can see
the answer without opening the browser.

## What it will not do

**Invent anything.** A field that could not be sourced is written as `null`, the
analyzer shows `N/A`, and the gate that depended on it stays `UNKNOWN`. Outside
US market hours some contracts have no two-sided quote, so the spread comes back
null — that is the honest answer, not a gap to fill with a zero.

**Treat a straddle as an earnings jump when no earnings are in the window.** The
at-the-money straddle prices the expected move over the whole life of the
contract. It is only a *jump* if the report actually falls inside that life. When
it does not, `impliedMovePct` stays null and the figure is reported separately as
`straddleMovePct`. Stripping a jump that is not in the IV would understate the
diffusive vol and make a fair trade look bad.

## IV Rank and IV Percentile

No free source publishes a history of implied vol, so neither can be fetched.
Every run appends today's at-the-money IV to `iv_history/{SYMBOL}.csv`. IV
Percentile appears once 60 observations have accumulated, IV Rank at 120. Until
then both are null, and you can type them in from your broker instead — anything
entered by hand wins.

Run it daily, even on days you are not trading, and the log builds itself.

## Where the data comes from, and what was rejected

`../docs/MARKET_DATA_APIS.md` has the measurements: what CBOE returns, which
Webull endpoints are open and which are blocked, and why Yahoo's option chain is
not used despite being the obvious first choice.

## Example output

`example/AAPL_vrp.json` is a real run, kept so you can see the shape without
running anything. The `ivRank`, `iv1yLow`, `iv1yHigh` and `ivPercentile` fields
are null in it because the IV log was one day old at the time.
