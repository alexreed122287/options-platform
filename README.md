# Options Platform

AI-assisted options trading platform: market regime scoring, contract
scanning and ranking, and a confirm-gated paper/live order flow on Alpaca,
with FMP for fundamentals and market data.

**Safety first:** starts in Alpaca PAPER mode. No order is ever submitted
without an explicit confirmation step in the UI - even on paper. Live mode
requires two extra, deliberate switches (see Going Live).

## Quick start

```
cd ~/options-platform
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and add your keys, then:

```
.venv/bin/python -m data.smoke_test
.venv/bin/python run.py
```

Open http://127.0.0.1:8787

The smoke test prints SPY quote, an SPY call chain with greeks, an FMP
profile, account info, cache stats, and remaining rate budgets. Providers
without keys are skipped gracefully.

## .env configuration

| Variable | Default | Purpose |
|---|---|---|
| `FMP_API_KEY` | (none) | Financial Modeling Prep key - quotes, sectors, VIX, history |
| `ALPACA_API_KEY` | (none) | Alpaca key id (paper keys from the Paper account view) |
| `ALPACA_SECRET_KEY` | (none) | Alpaca secret |
| `ALPACA_PAPER` | `true` | `true` = paper endpoint, `false` = real money endpoint |
| `LIVE_TRADING_ENABLED` | `false` | Second gate for live orders; ignored while paper |
| `ALPACA_DATA_FEED` | `iex` | Stock data feed; `sip` if your plan includes it |
| `ALPACA_OPTIONS_FEED` | `indicative` | Options feed; `opra` if your plan includes it |
| `FMP_BASE_URL` | financialmodelingprep.com | Override for testing |
| `PORT` | `8787` | Server port |
| `DB_PATH` | `options_platform.db` | SQLite location |

`.env` is gitignored. Keys are never logged; URLs and error bodies are
redacted before they reach any log line.

## Architecture

```
run.py                  server entrypoint
config/
  settings.json         cache TTLs, rate budgets, risk sizing, scan loop
  regime.json           regime component weights and thresholds
  scoring.json          score weights, delta/DTE bands, filters, top_n
  universe.json         tickers scanned (also the breadth sample)
data/                   provider layer
  fmp_client.py         FMP stable endpoints with legacy /api/v3 fallback
  alpaca_client.py      trading + market data APIs, merged option chains
  cache.py              TTL cache (keeps stale entries for outage fallback),
                        per-provider rolling rate budgets with logging
  base.py               shared HTTP, secret redaction, provider health
  smoke_test.py         python -m data.smoke_test
engine/                 pure math + orchestration
  indicators.py         EMA / trend structure
  regime.py             0-100 regime composite (SPY trend, breadth, VIX,
                        sector rotation), daily snapshots to SQLite
  scoring.py            deterministic component scores + universe Scanner
  alerts.py             background scan loop -> alerts table (notify only)
api/
  app.py                FastAPI app, serves the dashboard at /
  routes_market.py      /api/health /api/regime /api/recommendations /api/watchlist
  routes_trading.py     /api/account /api/positions /api/orders /api/journal /api/alerts
  db.py                 SQLite schema and helpers
web/
  index.html            single-page dashboard, vanilla JS, no build step
tests/
  test_scoring.py       deterministic scoring-math tests
```

Data flow: clients return `Fetched(data, stale, as_of)` wrappers. When a
provider call fails and a previous value exists in cache, the stale value is
served with `stale=true`, which the dashboard surfaces as a banner - never a
silent failure. Hard failures surface as `degraded` entries with reasons.

## How scoring works

`GET /api/recommendations` scans `config/universe.json`. Per underlying:
spot + EMA trend, then the call chain inside the configured DTE/strike
window. Each contract that passes the liquidity filters is scored 0-100 as a
weighted blend (weights in `config/scoring.json`):

- `delta_fit` - |delta| inside the target band
- `extrinsic` - extrinsic premium as a fraction of mid (lower is better)
- `spread` - (ask-bid)/mid tightness
- `open_interest`, `volume` - liquidity credit
- `iv_rank` - today's ATM IV percentile vs trailing 90 days (stored per scan
  in `iv_history`; neutral 0.5 until enough history accumulates)
- `dte_fit` - days to expiry inside the target band
- `trend_alignment` - underlying trend blended with the market regime

The API returns every component's raw value, score, weight, and contribution
so the dashboard can show the full breakdown per row.

The regime composite (`GET /api/regime`) weighs SPY 10/20/50 EMA structure,
breadth (universe + sectors positive), VIX level and direction, and
offensive-vs-defensive sector rotation; thresholds in `config/regime.json`.
Daily snapshots persist to `regime_snapshots`.

## Order flow and journal

Review Order (dashboard) -> `POST /api/orders/preview` returns mid, account
equity, and a risk-based size suggestion: `max_risk_pct_equity` percent of
equity (see `config/settings.json`) divided by contract cost. Confirming
calls `POST /api/orders` which requires `confirmed: true` - requests without
it are rejected with HTTP 400 in every mode. Each submitted order is
auto-journaled with the regime label/score and the contract's score
breakdown at entry. The Journal tab supports notes, order-status sync,
close-with-exit-price, and shows win rate, profit factor, net P&L, and
average hold.

## Alerts

A background loop (interval, threshold, and market-hours gating in
`config/settings.json` under `scan`) rescans the universe and records one
alert per contract per day when a score crosses the threshold. Alerts appear
as dashboard toasts and in the Alerts panel, and persist to the `alerts`
table. The loop only notifies - it cannot submit orders.

## Testing

```
.venv/bin/python -m pytest -q
```

## Troubleshooting

- **FMP 401/403** - check the key; new FMP accounts use the `stable`
  endpoints (default here), old accounts may only have legacy `/api/v3`
  (automatic fallback covers both).
- **Alpaca 403 on data** - your plan may not include the requested feed.
  Defaults (`iex`, `indicative`) work on free plans.
- **Empty recommendations** - check market hours (snapshots are thin
  overnight), the setup banner for missing keys, or loosen
  `config/scoring.json` filters.
- **Rate budget warnings in logs** - raise TTLs in `config/settings.json`
  or trim the universe.

## Going live checklist

Work through ALL of these, in order, before flipping any switch:

1. Run paper for at least several weeks; use the Journal stats (win rate,
   profit factor) to confirm the strategy and the platform behave as
   expected, including fills, syncs, and alerts.
2. Verify options trading approval and buying power on the LIVE Alpaca
   account (the dashboard shows the options approval level on preview).
3. Review `config/settings.json` risk caps: `max_risk_pct_equity` and
   `max_contracts_per_order` are your blast radius.
4. Put your LIVE keys in `.env` (`ALPACA_API_KEY` / `ALPACA_SECRET_KEY`).
5. Set `ALPACA_PAPER=false` in `.env`.
6. Set `LIVE_TRADING_ENABLED=true` in `.env`. Until you do, the server
   refuses live orders with HTTP 403 even if you confirm in the UI.
7. Restart the server and confirm the header badge reads LIVE.
8. Every live order additionally requires typing LIVE in the Review Order
   modal - the confirm button stays disabled without it.
9. Start with 1-contract orders and verify the first fill, journal entry,
   and position P&L end to end before sizing up.
10. Keep `.env` out of backups/screenshots; rotate keys if ever exposed.

## Safety invariants (do not weaken)

- `POST /api/orders` rejects anything without `confirmed: true` - paper
  included. The UI confirmation step is mandatory by construction.
- Live orders require `ALPACA_PAPER=false` AND `LIVE_TRADING_ENABLED=true`
  AND the typed `LIVE` acknowledgment per order.
- The alert loop and every other background path can read but never trade.
- All thresholds and weights live in `config/*.json`, hot-reloaded on edit.
