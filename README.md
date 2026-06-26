# Options Platform

AI-assisted options trading platform: market regime scoring, contract
scanning and ranking, and a confirm-gated order flow on your choice of
brokerage - Alpaca (paper or live), Tradier (sandbox or live), or Public.com
- with FMP for fundamentals and market data. Keys and broker selection are
editable from the dashboard's Settings tab.

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

## Entering keys (Settings tab)

Open the dashboard and go to **Settings** to enter or update your FMP, Alpaca,
Tradier, and Public keys, pick the **broker** (who executes orders) and **data
source** (who feeds the scanner), and toggle paper/live - all without editing
files or restarting. Changes write to your local `.env` (gitignored) and apply
immediately. Stored secret values are never shown back; a field left blank
keeps the existing key. The Settings tab is read-only in the hosted demo.

You can still set everything via `.env` directly if you prefer.

## Scanning for opportunities

The **Recommendations** tab (`GET /api/recommendations`) scans your universe
and ranks every qualifying contract top-to-bottom by a 0-100 score, with an
expandable per-contract breakdown. It needs a **data source with options
chains** - Alpaca, Tradier, or Public - so set one of those keys first. FMP
alone powers the regime score but not the chain scan.

### Universe and the prefilter funnel

`config/universe.json` ships the Option Panda / Finviz universe (avg vol >
400K, price > $1) with the **Healthcare** sector removed - ~2,587 names.
Deep-scanning that many option chains on every refresh is infeasible, so the
scanner uses a two-stage funnel:

1. **Prefilter (cheap):** batch-quote the whole universe through the active
   data source (Tradier/Alpaca handle large symbol lists in a handful of
   calls) and rank each name on trend, day momentum, and liquidity. Cached
   for `prefilter.cache_seconds` (default 30 min).
2. **Chain scan (expensive):** only the top `prefilter.max_chain_scan`
   (default 40) names get the full option-chain analysis.

Tune it in `config/universe.json`: raise `max_chain_scan` for deeper coverage
(more API calls, slower refresh), lower it for speed. Set `prefilter.enabled`
to `false` only for a small custom universe. The scan info line shows
"N tickers prefiltered to top M" so the funnel is always visible.

Results are **grouped by ticker**: each row is a ticker's single best-scoring
contract, so one name can't flood the list. Click a ticker to expand its full
option ladder ranked by score (up to `scoring.max_contracts_per_ticker`), then
click any option for its component breakdown. `scoring.top_n` is the number of
tickers shown.

### Segmenting by sector / theme

`config/segments.json` maps every universe ticker to its Finviz **sector**
(10, one per ticker - Healthcare removed) and the Finviz Elite **themes** it
belongs to (~400, many per ticker - "AI - Compute & Acceleration", "Quantum
Computing", "Nuclear", etc.). The Recommendations tab has a multi-select
sector picker and a theme picker; the scan restricts the universe to the
chosen segments *before* prefiltering, so you get the best opportunities
within that slice. `GET /api/segments` lists them with counts;
`GET /api/recommendations?sector=Technology&sector=Energy&theme=...` scans
those. An optional `price_min` / `price_max` (also editable in Settings)
filters by the underlying share price.
Each result row shows its sector.

### Targeting expiry (DTE filter)

A DTE dropdown re-targets the scan's days-to-expiry. By default the band is
25-60d, so near-term monthlies dominate; pick **Closer (7-25d)**, **Swing
(45-90d)**, **Further out (90-180d)**, or **LEAPS (180-400d)** to bias toward
shorter or longer-dated contracts. The selection overrides `dte_band` for both
the chain fetch window and dte_fit scoring (`GET /api/recommendations?dte=leaps`).
Presets live in `config/scoring.json` under `dte_presets`. Long-dated picks are
naturally thinner - fewer pass the OI/spread liquidity filters. Regenerate the maps from a fresh Finviz
export by re-running the import (sector from the CSV, themes from
`industry/master_tickers.json`).

> The prefilter quotes through your **data source**, not FMP - FMP plans cap
> quote volume and cannot price thousands of names. Use Tradier or Alpaca as
> the data source for full-universe ranking.

## Live demo (no keys, no setup)

The dashboard is phone-friendly and ships with a built-in demo mode: opened
from GitHub Pages (or any URL with `?demo=1`) it runs entirely in the
browser on realistic sample data. Order submission is disabled in the demo.
Once Pages is enabled for this repo the demo lives at:

https://alexreed122287.github.io/options-platform/

**GitHub Pages is static hosting** - it serves the HTML only, with no backend
and no keys, so it can *only* ever show demo data. For a real, live dashboard
you run the backend (locally, or on GitHub Codespaces below).

## Run the real dashboard on GitHub Codespaces (public URL, no local machine)

Codespaces runs the actual FastAPI server in the cloud and gives you a
forwarded URL with YOUR live data - all from this repo.

1. **Add your keys as Codespaces secrets** (one time): GitHub -> Settings ->
   Codespaces -> Secrets, scoped to this repo. They are injected as env vars,
   so no `.env` is needed. Add the ones you use:
   `FMP_API_KEY`, `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `TRADIER_ACCESS_TOKEN`,
   `BROKER`, `DATA_SOURCE`, and a long random `DASHBOARD_TOKEN`.
2. On the repo: **Code -> Codespaces -> Create codespace on main**. It installs
   dependencies automatically (`.devcontainer/`).
3. In the Codespace terminal: `python run.py`
4. **Ports** tab -> port 8787 -> right-click -> **Port Visibility -> Public**
   (the `DASHBOARD_TOKEN` gate keeps it private to anyone without the token).
5. Open the forwarded URL with your token:
   `https://<your-codespace>-8787.app.github.dev/?key=YOUR_DASHBOARD_TOKEN`

Notes: a Codespace **spins down after ~30 min idle** (restart it from the repo
to resume) and counts against your monthly Codespaces hours. Orders stay
double-gated (`LIVE_TRADING_ENABLED` + typed `LIVE`), so a shared link can read
but not trade. To share the tool without any keys or exposure, use the Pages
demo above instead.

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
| `TRADIER_ACCESS_TOKEN` | (none) | Tradier token (dashboard.tradier.com -> Settings -> API Access) |
| `TRADIER_ENV` | `production` | `sandbox` = paper (needs a sandbox token), `production` = real money |
| `TRADIER_ACCOUNT_ID` | auto | Override Tradier account number (auto-discovered otherwise) |
| `PUBLIC_API_SECRET` | (none) | Public.com API secret (public.com Settings -> Security -> API) |
| `BROKER` | `alpaca` | Order execution + account: `alpaca`, `tradier`, or `public` |
| `DATA_SOURCE` | `alpaca` | Scanner data (chains/greeks/spot): `alpaca`, `tradier`, or `public` |
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
  tradier_client.py     Tradier: bearer auth, sandbox/production, chains with
                        greeks, quote-enriched positions, form-encoded orders
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

| | Alpaca | Tradier | Public |
|---|---|---|---|
| Paper trading | yes (default) | yes (`TRADIER_ENV=sandbox`) | NO - real money only |
| Options chain + greeks | snapshots API (`indicative` feed) | chain API with ORATS greeks | per-expiration chain API |
| Historical bars (trend) | yes | yes | no - FMP history fills in |
| Market clock | yes | yes | no - ET window fallback |
| Order placement | synchronous status | synchronous status | asynchronous |

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
  Tradier with `TRADIER_ENV=production`, or Public always (no paper mode).
- The Settings API only writes an allowlisted set of env vars, sanitizes
  values, and never returns stored secrets - and it sits behind the same
  `DASHBOARD_TOKEN` gate as every other route.
- The alert loop and every other background path can read but never trade.
- All thresholds and weights live in `config/*.json`, hot-reloaded on edit.
