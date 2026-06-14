"""Financial Modeling Prep client: quotes, profiles, sector performance,
market breadth inputs, VIX, and EOD history.

Uses the current "stable" endpoints with automatic fallback to the legacy
/api/v3 paths where the two differ, since older FMP accounts may only have
one or the other.
"""
import datetime as dt
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .base import BaseClient, ProviderError
from .cache import Fetched, RateBudget, TTLCache
from .env import env, secret

log = logging.getLogger("data.fmp")

DEFAULT_BASE = "https://financialmodelingprep.com"


def _num(value: Any) -> Optional[float]:
    """Normalize FMP numeric fields that may arrive as '1.23%' strings."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.replace("%", "").replace(",", "").strip()
        if not value:
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _norm_quote(d: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    return {
        "symbol": d.get("symbol", symbol),
        "price": _num(d.get("price")),
        "change": _num(d.get("change")),
        "change_pct": _num(d.get("changePercentage", d.get("changesPercentage"))),
        "volume": _num(d.get("volume")),
        "prev_close": _num(d.get("previousClose")),
        "ma50": _num(d.get("priceAvg50")),
        "ma200": _num(d.get("priceAvg200")),
        "day_low": _num(d.get("dayLow")),
        "day_high": _num(d.get("dayHigh")),
        "ts": d.get("timestamp"),
    }


class FMPClient(BaseClient):
    name = "fmp"

    def __init__(self, cache: TTLCache, budget: RateBudget, ttls: Dict[str, float]):
        super().__init__(cache, budget)
        self._ttls = ttls
        self.base = (env("FMP_BASE_URL", DEFAULT_BASE) or DEFAULT_BASE).rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(secret("FMP_API_KEY"))

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        if not self.configured:
            raise ProviderError("fmp: FMP_API_KEY not set in .env")
        merged = dict(params or {})
        merged["apikey"] = secret("FMP_API_KEY")
        return await self._request_json("GET", f"{self.base}{path}", params=merged)

    async def _get_with_fallback(
        self, attempts: Sequence[Tuple[str, Optional[Dict[str, Any]]]]
    ) -> Any:
        """Try each (path, params) in order; return the first non-empty payload."""
        last_exc: Optional[ProviderError] = None
        for path, params in attempts:
            try:
                payload = await self._get(path, params)
            except ProviderError as exc:
                last_exc = exc
                continue
            if payload:
                return payload
        if last_exc is not None:
            raise last_exc
        raise ProviderError(f"fmp: empty response from {attempts[0][0]}")

    # ------------------------------------------------------------------ API

    async def quote(self, symbol: str) -> Fetched:
        async def _fetch() -> Dict[str, Any]:
            rows = await self._get_with_fallback([
                ("/stable/quote", {"symbol": symbol}),
                (f"/api/v3/quote/{symbol}", None),
            ])
            if isinstance(rows, dict):
                rows = [rows]
            if not rows:
                raise ProviderError(f"fmp: no quote returned for {symbol}")
            return _norm_quote(rows[0], symbol)

        return await self.cache.get_or_fetch(
            f"fmp:quote:{symbol}", self._ttls["quote"], _fetch
        )

    async def batch_quotes(self, symbols: List[str]) -> Fetched:
        """Quotes for many symbols. Tries the one-call batch endpoints first;
        some FMP plans reject those (403), so it falls back to per-symbol
        quotes - more requests, fully cached, same result shape."""
        joined = ",".join(symbols)

        async def _fetch() -> Dict[str, Dict[str, Any]]:
            out: Dict[str, Dict[str, Any]] = {}
            try:
                rows = await self._get("/stable/batch-quote", {"symbols": joined})
                for row in rows or []:
                    norm = _norm_quote(row, row.get("symbol", ""))
                    if norm["symbol"]:
                        out[norm["symbol"]] = norm
            except ProviderError as exc:
                log.info("batch quote endpoint unavailable (%s); "
                         "falling back to per-symbol quotes", str(exc)[:80])
            # Per-symbol fallback only for SMALL lists (breadth sample,
            # watchlist). Never fan out thousands of calls on a limited plan.
            if not out and len(symbols) <= 25:
                for symbol in symbols:
                    try:
                        fetched = await self.quote(symbol)
                        out[symbol] = fetched.data
                    except ProviderError as exc:
                        log.warning("quote unavailable for %s: %s", symbol, str(exc)[:80])
            if not out:
                raise ProviderError(
                    f"fmp: no quotes for batch of {len(symbols)} "
                    "(batch endpoint unavailable on this plan)"
                )
            return out

        return await self.cache.get_or_fetch(
            f"fmp:batch:{joined}", self._ttls["quote"], _fetch
        )

    async def profile(self, symbol: str) -> Fetched:
        async def _fetch() -> Dict[str, Any]:
            rows = await self._get_with_fallback([
                ("/stable/profile", {"symbol": symbol}),
                (f"/api/v3/profile/{symbol}", None),
            ])
            if isinstance(rows, dict):
                rows = [rows]
            if not rows:
                raise ProviderError(f"fmp: no profile returned for {symbol}")
            d = rows[0]
            description = d.get("description") or ""
            return {
                "symbol": d.get("symbol", symbol),
                "company_name": d.get("companyName"),
                "sector": d.get("sector"),
                "industry": d.get("industry"),
                "market_cap": _num(d.get("marketCap", d.get("mktCap"))),
                "beta": _num(d.get("beta")),
                "exchange": d.get("exchange"),
                "description": description[:300],
            }

        return await self.cache.get_or_fetch(
            f"fmp:profile:{symbol}", self._ttls["profile"], _fetch
        )

    async def sector_performance(self) -> Fetched:
        """Day performance per sector, normalized to [{sector, change_pct}]."""

        async def _fetch() -> List[Dict[str, Any]]:
            # stable snapshot (current API) first, most recent few days; the
            # legacy /api/v3 endpoint only as a last resort for old accounts
            attempts: List[Tuple[str, Optional[Dict[str, Any]]]] = []
            today = dt.date.today()
            for back in range(5):
                day = (today - dt.timedelta(days=back)).isoformat()
                attempts.append(("/stable/sector-performance-snapshot", {"date": day}))
            attempts.append(("/api/v3/sectors-performance", None))
            rows = await self._get_with_fallback(attempts)
            out: List[Dict[str, Any]] = []
            for row in rows or []:
                sector = row.get("sector")
                change = _num(row.get("changesPercentage", row.get("averageChange")))
                if sector is not None and change is not None:
                    out.append({"sector": sector, "change_pct": change})
            if not out:
                raise ProviderError("fmp: sector performance returned no usable rows")
            return out

        return await self.cache.get_or_fetch(
            "fmp:sectors", self._ttls["sector_performance"], _fetch
        )

    async def vix(self) -> Fetched:
        return await self.quote("^VIX")

    async def history(self, symbol: str, days: int = 120) -> Fetched:
        """Daily closes, ascending by date: [{date, close}]."""
        from_date = (dt.date.today() - dt.timedelta(days=int(days * 1.7) + 5)).isoformat()

        async def _fetch() -> List[Dict[str, Any]]:
            payload = await self._get_with_fallback([
                ("/stable/historical-price-eod/full", {"symbol": symbol, "from": from_date}),
                (f"/api/v3/historical-price-full/{symbol}", {"from": from_date}),
            ])
            if isinstance(payload, dict):
                rows = payload.get("historical") or []
            else:
                rows = payload or []
            out = []
            for row in rows:
                close = _num(row.get("close", row.get("price")))
                date = row.get("date")
                if date and close is not None:
                    out.append({"date": date[:10], "close": close})
            if not out:
                raise ProviderError(f"fmp: no history returned for {symbol}")
            out.sort(key=lambda r: r["date"])
            return out[-days:]

        return await self.cache.get_or_fetch(
            f"fmp:history:{symbol}:{days}", self._ttls["history"], _fetch
        )
