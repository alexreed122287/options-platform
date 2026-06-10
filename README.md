# Options Platform

AI-assisted options trading platform: market regime scoring, contract
scanning and ranking, and a confirm-gated order flow on your choice of
brokerage - Alpaca (paper or live) or Public.com - with FMP for fundamentals
and market data.

**Safety first:** starts in Alpaca PAPER mode. No order is ever submitted
without an explicit confirmation step in the UI - even on paper. Live mode
requires two extra, deliberate switches (see Going Live).

**Public.com is always real money.** Public's API has no paper environment,
so the platform treats `BROKER=public` as live: every order requires
`LIVE_TRADING_ENABLED=true` AND the typed LIVE acknowledgment, no exceptions.

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

## Live demo (no keys, no setup)

The dashboard is phone-friendly and ships with a built-in demo mode: opened
from GitHub Pages (or any URL with `?demo=1`) it runs entirely in the
browser on realistic sample data. Order submission is disabled in the demo.
Once Pages is enabled for this repo the demo lives at:

https://alexreed122287.github.io/options-platform/

## Use it from your iPhone (same Wi-Fi as the server)

1. Add a long random `DASHBOARD_TOKEN` to `.env` - with the server exposed
   on your network, the API (including order endpoints) must not be open.
2. Start the server bound to all interfaces:

```
cd ~/options-platform
HOST=0.0.0.0 .venv/bin/python run.py
```

3. Find your Mac's address:

```
ipconfig getifaddr en0
```

4. On the phone open `http://YOUR_MAC_IP:8787/?key=YOUR_DASHBOARD_TOKEN`.
   The token is remembered after the first load. Add to Home Screen in
   Safari for an app-like full-screen experience.

The UI confirmation gates apply from the phone exactly as on desktop.

## .env configuration

| Variable | Default | Purpose |
|---|---|---|
| `FMP_API_KEY` | (none) | Financial Modeling Prep key - quotes, sectors, VIX, history |
| `ALPACA_API_KEY` | (none) | Alpaca key id (paper keys from the Paper account view) |
| `ALPACA_SECRET_KEY` | (none) | Alpaca secret |
| `ALPACA_PAPER` | `true` | `true` = paper endpoint, `false` = real money endpoint |
| `PUBLIC_API_SECRET` | (none) | Public.com API secret (public.com Settings -> Security -> API) |
| `BROKER` | `alpaca` | Order execution + account: `alpaca` or `public` (Public = real money only) |
| `DATA_SOURCE` | `alpaca` | Scanner data (chains/greeks/spot): `alpaca` or `public` |
| `PUBLIC_ACCOUNT_ID` | auto | Override Public brokerage account id (auto-discovered otherwise) |
| `LIVE_TRADING_ENABLED` | `false` | Second gate for live orders; ignored while paper |
| `ALPACA_DATA_FEED` | `iex` | Stock data feed; `sip` if your plan includes it |
| `ALPACA_OPTIONS_FEED` | `indicative` | Options feed; `opra` if your plan includes it |
| `FMP_BASE_URL` | financialmodelingprep.com | Override for testing |
| `HOST` | `127.0.0.1` | Bind address; `0.0.0.0` to reach it from your phone on LAN |
| `DASHBOARD_TOKEN` | (none) | Shared secret required on every request when set - use with `HOST=0.0.0.0` |
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
  public_client.py      Public.com: JWT auth from API secret, portfolio,
                        per-expiration chains with greeks, async orders
  cache.py              TTL cache (keeps stale entries for outage fallback),
                        per-provider rolling rate budgets with logging
  base.py               shared HTTP, secret + runtime-token redaction, health
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

### Brokers and data sources

`BROKER` picks who holds the account and executes orders; `DATA_SOURCE`
picks who feeds the scanner. Any combination works:

| | Alpaca | Public |
|---|---|---|
| Paper trading | yes (default) | NO - real money only |
| Options chain + greeks | snapshots API (`indicative` feed) | per-expiration chain API |
| Historical bars (trend) | yes | no - FMP history fills in automatically |
| Market clock | yes | no - ET 9:30-16:00 weekday fallback |
| Order placement | synchronous status | asynchronous (submission != execution) |

Notes for `DATA_SOURCE=public`: chains are fetched one expiration at a time
(capped by `public.max_expirations_per_chain` in `config/settings.json`), and
per-ticker trend falls back to FMP history since Public has no bars endpoint.
Option symbols are normalized to compact OCC everywhere (OSI padding from
Public responses is stripped).

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

## Sharing this repo

- **Send the demo link** (above) - works on any phone, sample data only.
- **Share the code**: others clone the repo, create their own `.env` with
  their own keys, and run locally. Secrets and the SQLite journal are
  gitignored - they are never part of the repo.
- **Private repo + specific people**: `gh api -X PUT
  repos/alexreed122287/options-platform/collaborators/THEIR_USERNAME`
- The GitHub Pages demo requires the repo to be public on a free plan.

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
- **Public 401s** - the JWT is auto-refreshed (one retry per request); if it
  persists, regenerate the API secret at public.com and update `.env`.
- **Public scan feels slow** - chains are per-expiration; lower
  `public.max_expirations_per_chain` or tighten the DTE band in
  `config/scoring.json`.

## Going live checklist

Work through ALL of these, in order, before flipping any switch.

**If you are going live via Public.com:** there is no paper rehearsal at
Public - validate the whole workflow on Alpaca paper first (same UI, same
scoring, same order flow), confirm options trading is enabled on your Public
account, then set `BROKER=public`, `LIVE_TRADING_ENABLED=true`, and keep
`max_contracts_per_order` at 1 for the first trades. Public order placement
is asynchronous - use the Journal Sync button to confirm fills.

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
- Live orders require `LIVE_TRADING_ENABLED=true` AND the typed `LIVE`
  acknowledgment per order. "Live" means Alpaca with `ALPACA_PAPER=false`,
  or Public always - Public has no paper mode and is never treated as one.
- The alert loop and every other background path can read but never trade.
- All thresholds and weights live in `config/*.json`, hot-reloaded on edit.
