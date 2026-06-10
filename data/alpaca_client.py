"""Alpaca client.

Trading API (paper or live host): account, positions, clock, option contract
metadata (open interest), and order submission.
Market Data API: stock snapshots/bars, option snapshots with greeks and IV.

Order submission is ONLY reachable through the confirm-gated API route; no
other code path may call submit_order.
"""
import datetime as dt
import logging
from typing import Any, Dict, List, Optional

from .base import BaseClient, ProviderError
from .cache import Fetched, RateBudget, TTLCache
from .env import env, env_bool, secret

log = logging.getLogger("data.alpaca")

LIVE_TRADING_BASE = "https://api.alpaca.markets"
PAPER_TRADING_BASE = "https://paper-api.alpaca.markets"
DATA_BASE = "https://data.alpaca.markets"

# Hard cap on pagination per request type so one chain fetch cannot eat the
# whole rate budget.
MAX_PAGES = 4
PAGE_LIMIT = 1000


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class AlpacaClient(BaseClient):
    name = "alpaca"

    def __init__(
        self,
        cache: TTLCache,
        budget_trading: RateBudget,
        budget_data: RateBudget,
        ttls: Dict[str, float],
    ):
        super().__init__(cache, budget_trading)
        self.budget_data = budget_data
        self._ttls = ttls

    @property
    def configured(self) -> bool:
        return bool(secret("ALPACA_API_KEY")) and bool(secret("ALPACA_SECRET_KEY"))

    @property
    def paper(self) -> bool:
        return env_bool("ALPACA_PAPER", True)

    @property
    def trading_base(self) -> str:
        return PAPER_TRADING_BASE if self.paper else LIVE_TRADING_BASE

    def _headers(self) -> Dict[str, str]:
        if not self.configured:
            raise ProviderError("alpaca: ALPACA_API_KEY / ALPACA_SECRET_KEY not set in .env")
        return {
            "APCA-API-KEY-ID": secret("ALPACA_API_KEY") or "",
            "APCA-API-SECRET-KEY": secret("ALPACA_SECRET_KEY") or "",
            "Accept": "application/json",
        }

    async def _get_trading(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return await self._request_json(
            "GET", f"{self.trading_base}{path}", params=params, headers=self._headers()
        )

    async def _get_data(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return await self._request_json(
            "GET", f"{DATA_BASE}{path}", params=params, headers=self._headers(),
            budget=self.budget_data,
        )

    # --------------------------------------------------------- trading API

    async def account(self) -> Fetched:
        async def _fetch() -> Dict[str, Any]:
            raw = await self._get_trading("/v2/account")
            obp = raw.get("options_buying_power")
            return {
                "status": raw.get("status"),
                "currency": raw.get("currency"),
                "equity": _f(raw.get("equity")),
                "last_equity": _f(raw.get("last_equity")),
                "cash": _f(raw.get("cash")),
                "buying_power": _f(raw.get("buying_power")),
                "options_buying_power": _f(obp) if obp is not None else None,
                "options_approved_level": raw.get("options_approved_level"),
                "paper": self.paper,
            }

        return await self.cache.get_or_fetch("alpaca:account", self._ttls["account"], _fetch)

    async def positions(self) -> Fetched:
        async def _fetch() -> List[Dict[str, Any]]:
            raw = await self._get_trading("/v2/positions")
            out = []
            for p in raw or []:
                out.append({
                    "symbol": p.get("symbol"),
                    "asset_class": p.get("asset_class"),
                    "qty": _f(p.get("qty")),
                    "side": p.get("side"),
                    "avg_entry_price": _f(p.get("avg_entry_price")),
                    "current_price": _f(p.get("current_price")),
                    "market_value": _f(p.get("market_value")),
                    "cost_basis": _f(p.get("cost_basis")),
                    "unrealized_pl": _f(p.get("unrealized_pl")),
                    "unrealized_plpc": _f(p.get("unrealized_plpc")) * 100.0,
                })
            return out

        return await self.cache.get_or_fetch("alpaca:positions", self._ttls["positions"], _fetch)

    async def clock(self) -> Fetched:
        async def _fetch() -> Dict[str, Any]:
            raw = await self._get_trading("/v2/clock")
            return {
                "is_open": bool(raw.get("is_open")),
                "next_open": raw.get("next_open"),
                "next_close": raw.get("next_close"),
                "timestamp": raw.get("timestamp"),
            }

        return await self.cache.get_or_fetch("alpaca:clock", self._ttls["clock"], _fetch)

    async def _option_contracts(
        self,
        underlying: str,
        type_: str,
        exp_gte: Optional[str],
        exp_lte: Optional[str],
        strike_gte: Optional[float],
        strike_lte: Optional[float],
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {
            "underlying_symbols": underlying,
            "status": "active",
            "type": type_,
            "limit": PAGE_LIMIT,
        }
        if exp_gte:
            params["expiration_date_gte"] = exp_gte
        if exp_lte:
            params["expiration_date_lte"] = exp_lte
        if strike_gte is not None:
            params["strike_price_gte"] = str(round(strike_gte, 2))
        if strike_lte is not None:
            params["strike_price_lte"] = str(round(strike_lte, 2))
        rows: List[Dict[str, Any]] = []
        for _ in range(MAX_PAGES):
            raw = await self._get_trading("/v2/options/contracts", params)
            rows.extend(raw.get("option_contracts") or [])
            token = raw.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
        return rows

    async def _option_snapshots(
        self,
        underlying: str,
        type_: str,
        exp_gte: Optional[str],
        exp_lte: Optional[str],
        strike_gte: Optional[float],
        strike_lte: Optional[float],
    ) -> Dict[str, Dict[str, Any]]:
        params: Dict[str, Any] = {
            "feed": env("ALPACA_OPTIONS_FEED", "indicative"),
            "type": type_,
            "limit": PAGE_LIMIT,
        }
        if exp_gte:
            params["expiration_date_gte"] = exp_gte
        if exp_lte:
            params["expiration_date_lte"] = exp_lte
        if strike_gte is not None:
            params["strike_price_gte"] = str(round(strike_gte, 2))
        if strike_lte is not None:
            params["strike_price_lte"] = str(round(strike_lte, 2))
        snaps: Dict[str, Dict[str, Any]] = {}
        for _ in range(MAX_PAGES):
            raw = await self._get_data(f"/v1beta1/options/snapshots/{underlying}", params)
            snaps.update(raw.get("snapshots") or {})
            token = raw.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
        return snaps

    async def chain(
        self,
        underlying: str,
        type_: str = "call",
        exp_gte: Optional[str] = None,
        exp_lte: Optional[str] = None,
        strike_gte: Optional[float] = None,
        strike_lte: Optional[float] = None,
    ) -> Fetched:
        """Merged option chain: contract metadata (open interest) joined with
        snapshot quotes, greeks, and IV."""
        key = (
            f"alpaca:chain:{underlying}:{type_}:{exp_gte}:{exp_lte}:"
            f"{strike_gte}:{strike_lte}"
        )

        async def _fetch() -> List[Dict[str, Any]]:
            contracts = await self._option_contracts(
                underlying, type_, exp_gte, exp_lte, strike_gte, strike_lte
            )
            snaps = await self._option_snapshots(
                underlying, type_, exp_gte, exp_lte, strike_gte, strike_lte
            )
            today = dt.date.today()
            out: List[Dict[str, Any]] = []
            for c in contracts:
                occ = c.get("symbol")
                snap = snaps.get(occ) or {}
                quote = snap.get("latestQuote") or {}
                trade = snap.get("latestTrade") or {}
                greeks = snap.get("greeks") or {}
                bar = snap.get("dailyBar") or {}
                bid = _f(quote.get("bp"))
                ask = _f(quote.get("ap"))
                mid = round((bid + ask) / 2.0, 4) if bid > 0 and ask > 0 else None
                exp = c.get("expiration_date")
                try:
                    dte = (dt.date.fromisoformat(exp) - today).days if exp else None
                except ValueError:
                    dte = None
                out.append({
                    "occ_symbol": occ,
                    "underlying": underlying,
                    "type": type_,
                    "strike": _f(c.get("strike_price")),
                    "expiration": exp,
                    "dte": dte,
                    "bid": bid,
                    "ask": ask,
                    "mid": mid,
                    "last": _f(trade.get("p")) or None,
                    "volume": int(_f(bar.get("v"))),
                    "open_interest": int(_f(c.get("open_interest"))),
                    "iv": snap.get("impliedVolatility"),
                    "delta": greeks.get("delta"),
                    "gamma": greeks.get("gamma"),
                    "theta": greeks.get("theta"),
                    "vega": greeks.get("vega"),
                })
            return out

        return await self.cache.get_or_fetch(key, self._ttls["chain"], _fetch)

    # ------------------------------------------------------------ data API

    async def stock_snapshot(self, symbol: str) -> Fetched:
        async def _fetch() -> Dict[str, Any]:
            params = {"feed": env("ALPACA_DATA_FEED", "iex")}
            raw = await self._get_data(f"/v2/stocks/{symbol}/snapshot", params)
            trade = raw.get("latestTrade") or {}
            daily = raw.get("dailyBar") or {}
            prev = raw.get("prevDailyBar") or {}
            price = _f(trade.get("p")) or _f(daily.get("c"))
            prev_close = _f(prev.get("c"))
            change_pct = (
                round((price / prev_close - 1.0) * 100.0, 2)
                if price and prev_close else None
            )
            return {
                "symbol": symbol,
                "price": price or None,
                "prev_close": prev_close or None,
                "change_pct": change_pct,
                "day_volume": int(_f(daily.get("v"))),
                "ts": trade.get("t"),
            }

        return await self.cache.get_or_fetch(
            f"alpaca:snapshot:{symbol}", self._ttls["stock_snapshot"], _fetch
        )

    async def bars(self, symbol: str, days: int = 120) -> Fetched:
        """Daily closes, ascending: [{date, close}]."""
        async def _fetch() -> List[Dict[str, Any]]:
            start = (
                dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=int(days * 1.7) + 5)
            ).strftime("%Y-%m-%d")
            params = {
                "timeframe": "1Day",
                "start": start,
                "limit": 1000,
                "adjustment": "split",
                "feed": env("ALPACA_DATA_FEED", "iex"),
            }
            raw = await self._get_data(f"/v2/stocks/{symbol}/bars", params)
            bars = raw.get("bars") or []
            out = [
                {"date": str(b.get("t", ""))[:10], "close": _f(b.get("c"))}
                for b in bars
                if b.get("c") is not None
            ]
            if not out:
                raise ProviderError(f"alpaca: no bars returned for {symbol}")
            return out[-days:]

        return await self.cache.get_or_fetch(
            f"alpaca:bars:{symbol}:{days}", self._ttls["bars"], _fetch
        )

    async def option_latest_quote(self, occ_symbol: str) -> Dict[str, Any]:
        """Uncached latest quote for one contract (used by order preview)."""
        params = {
            "symbols": occ_symbol,
            "feed": env("ALPACA_OPTIONS_FEED", "indicative"),
        }
        raw = await self._get_data("/v1beta1/options/quotes/latest", params)
        q = (raw.get("quotes") or {}).get(occ_symbol) or {}
        bid = _f(q.get("bp"))
        ask = _f(q.get("ap"))
        mid = round((bid + ask) / 2.0, 4) if bid > 0 and ask > 0 else None
        return {"occ_symbol": occ_symbol, "bid": bid, "ask": ask, "mid": mid, "ts": q.get("t")}

    # -------------------------------------------------------------- orders

    async def submit_order(
        self, occ_symbol: str, qty: int, limit_price: float, side: str = "buy"
    ) -> Dict[str, Any]:
        """Submit a day limit order. Callers MUST have passed the confirm gate."""
        body = {
            "symbol": occ_symbol,
            "qty": str(int(qty)),
            "side": side,
            "type": "limit",
            "limit_price": str(round(float(limit_price), 2)),
            "time_in_force": "day",
        }
        log.info(
            "submitting %s order: %s x%s limit %s (paper=%s)",
            side, occ_symbol, qty, body["limit_price"], self.paper,
        )
        return await self._request_json(
            "POST", f"{self.trading_base}/v2/orders",
            json_body=body, headers=self._headers(),
        )

    async def get_order(self, order_id: str) -> Dict[str, Any]:
        return await self._get_trading(f"/v2/orders/{order_id}")
