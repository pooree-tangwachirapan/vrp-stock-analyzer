# Where to get option data, and what each source actually gives you

Findings from testing four sources on 2026-10-01, from a machine in Thailand, while the
US market was **closed** (05:57–06:35 ET, pre-market). Written down so nobody has to
repeat the investigation.

**Short version: use CBOE's public delayed-quote JSON.** It is the only free source
tested that returns implied vol, Greeks, open interest and two-sided quotes outside
regular trading hours, with no key and no account.

---

## 1. CBOE delayed quotes — the one to use

```
GET https://cdn.cboe.com/api/global/delayed_quotes/options/{SYMBOL}.json
```

No key, no login, no headers beyond a normal `User-Agent`. Gzipped.

**Symbol format**

| Underlying | Path | Result |
|---|---|---|
| Stock or ETF | `AAPL.json`, `TSLA.json`, `QQQ.json`, `SPY.json` | works |
| Index | `_SPX.json`, `_NDX.json`, `_VIX.json` (leading underscore) | works |
| Index without the underscore | `SPX.json` | **HTTP 403** |
| Unknown symbol | `ZZZZNOTREAL.json` | **HTTP 403** (fails cleanly) |

**Sizes measured** (every expiry and strike in one file)

| Symbol | Bytes | Contracts | Spot at the time |
|---|---|---|---|
| AAPL | 1,610,986 | 3,636 | 333.26 |
| TSLA | 2,287,700 | 5,122 | 355.93 |
| QQQ | 5,233,560 | 11,832 | 743.61 |
| SPY | 6,089,720 | 13,736 | 764.10 |
| _NDX | 7,140,772 | 16,410 | 30,408.50 |
| _SPX | 13,579,018 | 30,592 | 7,651.54 |
| _VIX | 714,779 | 1,620 | 16.50 |

**Response shape**

```jsonc
{
  "timestamp": "2026-10-01 10:04:05",          // per symbol, CDN-cached separately
  "symbol": "AAPL",
  "data": {
    "symbol": "AAPL", "security_type": "stock",
    "current_price": 333.26, "bid": …, "ask": …, "open": …,
    "price_change": …, "price_change_percent": …,
    "options": [ /* one object per contract */ ]
  }
}
```

Each contract carries:

```
option (OCC symbol)  bid  ask  bid_size  ask_size  volume  open_interest
iv  delta  gamma  theta  vega  rho  theo
last_trade_price  last_trade_time  open  high  low  prev_day_close  change  percent_change  tick
```

**Parsing the OCC symbol**

```python
re.match(r"^([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$", sym)
# root, YY, MM, DD, C|P, strike * 1000
# "AAPL261106P00315000" -> AAPL, 2026-11-06, Put, 315.0
```

**Quality, measured while the market was closed**

AAPL, 36 DTE expiry: **96 of 122 contracts had a real two-sided quote.** The implied
vol formed a proper smile (0.2744 / 0.2688 / 0.2616 / 0.2569 / 0.2535 across strikes),
and delta came straight from the feed (−0.2906, −0.3537, −0.4225 …), so finding the
delta-25 strike is a one-line interpolation rather than a Black-Scholes solve.

**Caveats**

- Quotes are delayed, and the timestamp is cached per symbol — two symbols fetched in
  the same minute can be stamped half an hour apart.
- Spreads look wide outside RTH. AAPL's delta-25 put showed an 18.18% spread, which
  correctly fails a 10% liquidity gate. Re-fetch during RTH for tradeable numbers.
- Files are large. `_SPX.json` is 13.6 MB; cache it rather than re-fetching per query.
- No implied vol history, so IV Rank and IV Percentile cannot be derived from it.

---

## 2. Webull — the chain is visible to a browser, the API is not

Host: `https://quotes-gw.webullfintech.com/api`

**Works with no authentication**

| Endpoint | Result |
|---|---|
| `GET /search/pc/tickers?keyword=AAPL&pageIndex=1&pageSize=5` | 200, 2,961 bytes. Returns `tickerId` (AAPL = `913256135`), `symbol`, `name`, `disExchangeCode` |
| `GET /stock/tickerRealTime/getQuote?tickerId=913256135&includeSecu=1&includeQuote=1` | 200, 1,509 bytes. `symbol`, `close` (333.02), `status`, `tradeTime` |
| `GET /bgw/quote/realtime?ids=913256135&includeSecu=1&includeQuote=1&more=1` | used by the site |
| `GET /quote/charts/queryMinutes?period=d1&tickerIds=913256135` | used by the site |
| `GET /market/region/id` | used by the site |

So **quotes and ticker lookup are open**. The ticker id is the key to everything else.

**Blocked — every option path, HTTP 417 `API_DISABLED`**

```
GET /quote/option/strategy/list?tickerId=…&count=-1&direction=all&expireCycle=&type=0
GET /quote/option/list?tickerId=…&count=-1&direction=all&type=0
GET /quote/option/{tickerId}/list?regionId=6
GET /quote/option/expireDate?tickerId=…
GET /quote/option/queryExpireDate?tickerId=…
GET /quote/option/queryOptionList?tickerId=…
POST /quote/option/quotes/queryBatch
```

All seven returned the **same `traceId` within one run**, which says the gateway rejects
the whole `/quote/option/*` namespace before routing, rather than each path failing
separately. Sending the full Webull header set (`did`, `platform`, `os`, `osv`, `app`,
`appid`, `ver`, `lzone`, `ph`, `locale`, `t_time`, `reqid`, `device-type`) did not help
on GET.

**The one that behaves differently**

```
POST https://quotes-gw.webullfintech.com/api/quote/option/strategy/list
Content-Type: application/json
{"tickerId": 913256135, "count": -1, "direction": "all", "expireCycle": [], "type": 0, "quoteType": 0}
```

returns **HTTP 200 with an empty body** — zero bytes — for three different body shapes.
It accepts the request and answers with nothing.

This is the endpoint the website itself uses (confirmed from
`performance.getEntriesByType('resource')` on the live options page: a `fetch` to
`/api/quote/option/strategy/list` with no query string). The site gets real data from it
while sending Webull's app-identity headers. The difference between 200-with-data and
200-with-nothing is therefore those headers.

**Why this repo does not use it.** Making a script work means presenting it as the
Webull web app so their gateway lets it through. That is working around an access
control they deliberately put in place — the `API_DISABLED` response *is* the control —
and Webull's terms prohibit automated access. Not worth building, and it would break the
first time they change the header contract.

**What is fine, and is genuinely good**

`https://www.webull.com/quote/nasdaq-aapl/options` shows the full chain **without
logging in**, 15 minutes delayed:

```
Strike	Bid	Ask	Last	% Change	Mid	Impl Vol	Open Int	Delta	Gamma	Theta
332.5	3.05	3.40	3.29	+63.68%	3.23	27.73%	4,971	0.5456	0.0652	-0.9517
335	1.85	2.14	1.95	+56.00%	2.00	27.47%	9,663	0.3828	0.0634	-0.8828
```

Tab-separated when copied, so a paste parser is easy. The ATM IV (27.73% at strike 332.5
against a 333.02 spot) is sane, where Yahoo gave 0.2% for the same contract.

Note the rows are rendered client-side: a plain `urllib` GET of that page returns 406 KB
containing the `Impl Vol` column header but none of the numbers, and there is no
`__NEXT_DATA__` block to mine.

---

## 3. Yahoo Finance via yfinance — good for prices, bad for chains

yfinance 1.5.2. Reachable from this machine; no proxy problem.

**Reliable**

- `Ticker.history(period=…)` — daily OHLC plus Adj Close
- `Ticker.options` — expiration list (22 for AAPL)
- `Ticker.get_earnings_dates(limit=…)` — 25 rows, past and future
- `Ticker.calendar` — earnings date, dividend dates, estimate ranges

**Option chain columns** (`Ticker.option_chain(expiry)`)

```
contractSymbol  lastTradeDate  strike  lastPrice  bid  ask  change  percentChange
volume  openInterest  impliedVolatility  inTheMoney  contractSize  currency
```

**No Greeks at all**, so the delta-25 strike has to be solved for with Black-Scholes.

**Measured quality, same moment as everything else above**

| | AAPL 2026-10-16 | NVDA 2026-10-16 |
|---|---|---|
| strikes with a two-sided quote | **5 / 69** | **3 / 81** |
| strikes with open interest > 0 | **1 / 69** | **3 / 81** |
| strikes with IV > 1% | 48 / 69 | 52 / 81 |

The near-the-money implied vols were `0.000010`, `0.001963`, `0.015635` — Yahoo's own
solver failing, not real values. `lastPrice` and `volume` were fine (stamped from the
previous close).

**Verdict.** Use it for price history, earnings dates and past earnings moves. Do not
use its option chain: outside RTH there are no quotes and no open interest, and the
`impliedVolatility` column cannot be trusted even when it is populated.

---

## 4. Everything else considered

| Source | Why it was not used |
|---|---|
| Tradier | Free sandbox gives delayed chains with Greeks, but needs a signup and a token |
| Polygon.io | Free tier is end-of-day; Greeks and IV are on paid tiers |
| Alpaca | Needs an account and a key |
| Interactive Brokers | Needs TWS or Gateway running plus a funded account |
| Schwab / thinkorswim | Needs an account and app approval |
| Finnhub, Twelve Data, Nasdaq Data Link | Option chains are a paid feature |
| Stooq | Daily prices only, no options |

Any of the first five is a reasonable upgrade if you already hold an account; they are
documented APIs with real support, and some carry IV history.

---

## 5. The one thing no free source provides

**IV Rank and IV Percentile.** Both need a time series of implied vol, and none of the
free sources publishes one. CBOE gives today's surface; Yahoo gives nothing usable;
Webull shows today only.

Two ways around it, and this repo does both:

1. Log it yourself. Every fetch appends the ATM IV to `fetch/iv_history/{SYMBOL}.csv`.
   IV Percentile becomes real once a few months have accumulated; a full year gives a
   proper IV Rank. Until then both report `N/A` and gate G5 stays `UNKNOWN` — which is
   the correct behaviour, not a bug.
2. Type it in. Most brokers show IV Rank and IV Percentile on the chain screen, so the
   fields stay editable and anything entered by hand wins over the log.
