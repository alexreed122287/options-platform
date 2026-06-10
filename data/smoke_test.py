"""Phase 1 CLI smoke test.

    python -m data.smoke_test

Exercises the FMP and Alpaca clients end to end: SPY quote, SPY option chain
with greeks, one FMP profile, account info, cache behavior, and rate budgets.

Providers without keys are reported as MISSING and skipped; a configured
provider that fails marks the run as failed (exit code 1). Secret values are
never printed.
"""
import asyncio
import datetime as dt
import json
import logging
import sys

from .alpaca_client import AlpacaClient
from .base import ProviderError
from .cache import RateBudget, TTLCache
from .env import ROOT, env, env_bool, load_env, secret
from .fmp_client import FMPClient


def _key_status(name: str) -> str:
    if secret(name):
        return "set"
    if env(name):
        return "PLACEHOLDER (edit .env with the real key)"
    return "MISSING"


def _settings() -> dict:
    with open(ROOT / "config" / "settings.json") as fh:
        return json.load(fh)


def section(title: str) -> None:
    print()
    print("=" * 64)
    print(title)
    print("=" * 64)


def build_clients(settings: dict):
    ttls = settings["cache_ttls_seconds"]
    budgets = settings["rate_budgets"]
    cache = TTLCache()
    fmp = FMPClient(cache, RateBudget("fmp", **budgets["fmp"]), ttls)
    alpaca = AlpacaClient(
        cache,
        RateBudget("alpaca_trading", **budgets["alpaca_trading"]),
        RateBudget("alpaca_data", **budgets["alpaca_data"]),
        ttls,
    )
    return cache, fmp, alpaca


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    load_env()
    settings = _settings()
    cache, fmp, alpaca = build_clients(settings)
    failures = []

    section("ENVIRONMENT")
    print(f"FMP_API_KEY:       {_key_status('FMP_API_KEY')}")
    print(f"ALPACA_API_KEY:    {_key_status('ALPACA_API_KEY')}")
    print(f"ALPACA_SECRET_KEY: {_key_status('ALPACA_SECRET_KEY')}")
    print(f"ALPACA_PAPER:      {env_bool('ALPACA_PAPER', True)}")
    print(f"LIVE_TRADING_ENABLED: {env_bool('LIVE_TRADING_ENABLED', False)}")

    section("FMP")
    if not fmp.configured:
        print("skipped: FMP_API_KEY missing")
    else:
        try:
            q = await fmp.quote("SPY")
            print(f"SPY quote: price={q.data['price']} change={q.data['change_pct']}% stale={q.stale}")
            await fmp.quote("SPY")
            print(f"SPY quote again -> cache: {cache.stats()}")
            p = await fmp.profile("AAPL")
            mcap = p.data["market_cap"]
            mcap_str = f"{mcap / 1e9:,.0f}B" if mcap else "n/a"
            print(f"AAPL profile: {p.data['company_name']} | {p.data['sector']} | mktcap={mcap_str}")
            s = await fmp.sector_performance()
            ranked = sorted(s.data, key=lambda r: r["change_pct"], reverse=True)
            shown = ", ".join(f"{r['sector']} {r['change_pct']:+.2f}%" for r in ranked[:3])
            print(f"Sectors ({len(ranked)}): top {shown}")
            v = await fmp.vix()
            print(f"VIX: {v.data['price']} ({v.data['change_pct']}%)")
        except ProviderError as exc:
            failures.append(f"fmp: {exc}")
            print(f"FAILED: {exc}")

    section("ALPACA")
    if not alpaca.configured:
        print("skipped: ALPACA_API_KEY / ALPACA_SECRET_KEY missing")
    else:
        try:
            acct = await alpaca.account()
            print(
                f"Account: status={acct.data['status']} equity=${acct.data['equity']:,.2f} "
                f"options_level={acct.data['options_approved_level']} paper={acct.data['paper']}"
            )
            snap = await alpaca.stock_snapshot("SPY")
            spot = snap.data["price"]
            print(f"SPY snapshot: price={spot} change={snap.data['change_pct']}%")
            if spot:
                today = dt.date.today()
                chain = await alpaca.chain(
                    "SPY", "call",
                    exp_gte=(today + dt.timedelta(days=20)).isoformat(),
                    exp_lte=(today + dt.timedelta(days=50)).isoformat(),
                    strike_gte=spot * 0.85,
                    strike_lte=spot * 1.05,
                )
                quoted = [c for c in chain.data if c["mid"]]
                print(f"SPY call chain: {len(chain.data)} contracts, {len(quoted)} with live quotes")
                with_delta = [c for c in quoted if c["delta"] is not None]
                with_delta.sort(key=lambda c: abs(c["delta"] - 0.7))
                print(f"{'contract':<24}{'strike':>8}{'dte':>5}{'bid':>8}{'ask':>8}{'delta':>7}{'iv':>7}{'oi':>7}{'vol':>6}")
                for c in with_delta[:5]:
                    iv = f"{c['iv']:.2f}" if c["iv"] is not None else "-"
                    print(
                        f"{c['occ_symbol']:<24}{c['strike']:>8.1f}{c['dte']:>5}"
                        f"{c['bid']:>8.2f}{c['ask']:>8.2f}{c['delta']:>7.2f}{iv:>7}"
                        f"{c['open_interest']:>7}{c['volume']:>6}"
                    )
        except ProviderError as exc:
            failures.append(f"alpaca: {exc}")
            print(f"FAILED: {exc}")

    section("CACHE + RATE BUDGETS")
    print(f"cache: {cache.stats()}")
    for budget in (fmp.budget, alpaca.budget, alpaca.budget_data):
        s = budget.snapshot()
        print(
            f"{s['name']}: {s['remaining_minute']}/{s['limit_minute']} per-minute remaining, "
            f"{s['remaining_day']}/{s['limit_day']} per-day remaining"
        )

    section("RESULT")
    if failures:
        print(f"{len(failures)} configured provider(s) FAILED:")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("Smoke test completed. Providers without keys were skipped gracefully.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
