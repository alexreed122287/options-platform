"""Tradier brokerage client.

Auth: a single bearer access token (TRADIER_ACCESS_TOKEN). Two environments:
  production (https://api.tradier.com)  - REAL MONEY
  sandbox    (https://sandbox.tradier.com) - paper, needs a sandbox token,
             delayed market data
TRADIER_ENV selects it (default production). paper is True only in sandbox,
so production Tradier gets the full live order gating.

Tradier response quirks handled here:
  - payloads wrap lists that collapse to a single object for one result
    (options.option, quotes.quote, positions.position, history.day)
  - order placement is form-encoded, not JSON
  - positions carry no live price, so P&L is enriched from a quotes call
  - greeks/IV come from ORATS under each contract's `greeks` (IV = mid_iv)

Docs: https://docs.tradier.com/reference
"""
import datetime as dt
import logging
import re
from typing import Any, Dict, List, Optional

from .base import BaseClient, ProviderError
from .cache import Fetched, RateBudget, TTLCache
from .env import env, secret

log = logging.getLogger("data.tradier")

PRODUCTION_BASE = "https://api.tradier.com"
SANDBOX_BASE = "https://sandbox.tradier.com"

# Tradier order status -> platform-normalized status
ORDER_STATUS_MAP = {
    "open": "new",
    "pending": "pending_new",
    "partially_filled": "partially_filled",
    "filled": "filled",
    "canceled": "canceled",
    "cancelled": "canceled",
    "expired": "expired",
    "rejected": "rejected",
    "error": "rejected",
}

_OCC_PARSE = re.compile(r"^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


def _f(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def listify(value: Any) -> List[Any]:
    """Tradier collapses single-element lists to a bare object; normalize."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def map_order_status(status: Optional[str]) -> Optional[str]:
    if not status:
        return None
    return ORDER_STATUS_MAP.get(status.lower(), status.lower())


def occ_underlying(occ: str) -> Optional[str]:
    match = _OCC_PARSE.match(occ.replace(" ", "").upper())
    return match.group(1) if match else None


def norm_chain_item(c: Dict[str, Any], underlying: str, today: dt.date) -> Dict[str, Any]:
    """Normalize a Tradier chain entry to the platform contract shape."""
    greeks = c.get("greeks") or {}
    bid = _f(c.get("bid")) or 0.0
    ask = _f(c.get("ask")) or 0.0
    mid = round((bid + ask) / 2.0, 4) if bid > 0 and ask > 0 else None
    exp = c.get("expiration_date")
    try:
        dte = (dt.date.fromisoformat(exp) - today).days if exp else None
    except ValueError:
        dte = None
    return {
        "occ_symbol": (c.get("symbol") or "").upper(),
        "underlying": underlying,
        "type": "call" if (c.get("option_type") or "").lower() == "call" else "put",
        "strike": _f(c.get("strike")) or 0.0,
        "expiration": exp,
        "dte": dte,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "last": _f(c.get("last")),
        "volume": int(_f(c.get("volume")) or 0),
        "open_interest": int(_f(c.get("open_interest")) or 0),
        "iv": _f(greeks.get("mid_iv")) or _f(greeks.get("smv_vol")),
        "delta": _f(greeks.get("delta")),
        "gamma": _f(greeks.get("gamma")),
        "theta": _f(greeks.get("theta")),
        "vega": _f(greeks.get("vega")),
    }


class TradierClient(BaseClient):
    name = "tradier"

    def __init__(self, cache: TTLCache, budget: RateBudget, ttls: Dict[str, float]):
        super().__init__(cache, budget)
        self._ttls = ttls
        self._account_id_cached: Optional[str] = None

    @property
    def configured(self) -> bool:
        return bool(secret("TRADIER_ACCESS_TOKEN"))

    @property
    def environment(self) -> str:
        return (env("TRADIER_ENV", "production") or "production").lower()

    @property
    def paper(self) -> bool:
        """Sandbox is Tradier's paper environment; production is real money."""
        return self.environment == "sandbox"

    @property
    def base(self) -> str:
        return SANDBOX_BASE if self.paper else PRODUCTION_BASE

    def _headers(self) -> Dict[str, str]:
        if not self.configured:
            raise ProviderError("tradier: TRADIER_ACCESS_TOKEN not set in .env")
        return {
            "Authorization": f"Bearer {secret('TRADIER_ACCESS_TOKEN')}",
            "Accept": "application/json",
        }

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return await self._request_json(
            "GET", f"{self.base}{path}", params=params, headers=self._headers()
        )

    async def _post_form(self, path: str, data: Dict[str, Any]) -> Any:
        return await self._request_json(
            "POST", f"{self.base}{path}", headers=self._headers(), data=data
        )

    # -------------------------------------------------------------- account

    async def _account_id(self) -> str:
        override = env("TRADIER_ACCOUNT_ID")
        if override:
            return override
        if self._account_id_cached:
            return self._account_id_cached
        raw = await self._get("/v1/user/profile")
        accounts = listify((raw.get("profile") or {}).get("account"))
        if not accounts:
            raise ProviderError("tradier: no account found for this token")
        self._account_id_cached = accounts[0].get("account_number")
        if not self._account_id_cached:
            raise ProviderError("tradier: profile returned no account_number")
        return self._account_id_cached

    async def account(self) -> Fetched:
        async def _fetch() -> Dict[str, Any]:
            acct_id = await self._account_id()
            raw = await self._get(f"/v1/accounts/{acct_id}/balances")
            bal = (raw.get("balance") or {}).get("balances") or raw.get("balances") or {}
            cash = bal.get("cash") or {}
            return {
                "status": "ACTIVE",
                "currency": "USD",
                "equity": _f(bal.get("total_equity")) or 0.0,
                "last_equity": None,
                "cash": _f(bal.get("total_cash")),
                "buying_power": _f(bal.get("stock_buying_power")) or _f(cash.get("cash_available")) or 0.0,
                "options_buying_power": _f(bal.get("option_buying_power")),
                "options_approved_level": None,
                "paper": self.paper,
                "broker": "tradier",
                "environment": self.environment,
            }

        return await self.cache.get_or_fetch("tradier:account", self._ttls["account"], _fetch)

    async def positions(self) -> Fetched:
        async def _fetch() -> List[Dict[str, Any]]:
            acct_id = await self._account_id()
            raw = await self._get(f"/v1/accounts/{acct_id}/positions")
            container = raw.get("positions")
            if not container or container == "null":
                return []
            rows = listify(container.get("position"))
            # Tradier positions carry no live price; enrich with one quotes call.
            symbols = [r.get("symbol") for r in rows if r.get("symbol")]
            prices: Dict[str, float] = {}
            if symbols:
                try:
                    quotes = await self._quotes(symbols, greeks=False)
                    prices = {q.get("symbol"): _f(q.get("last")) for q in quotes}
                except ProviderError as exc:
                    log.warning("tradier: position quote enrichment failed: %s", exc)
            out = []
            for r in rows:
                symbol = (r.get("symbol") or "").upper()
                qty = _f(r.get("quantity")) or 0.0
                cost_basis = _f(r.get("cost_basis")) or 0.0
                is_option = bool(_OCC_PARSE.match(symbol))
                mult = 100.0 if is_option else 1.0
                price = prices.get(symbol)
                market_value = price * qty * mult if price is not None else 0.0
                unrealized = market_value - cost_basis
                avg_entry = abs(cost_basis / (qty * mult)) if qty else 0.0
                out.append({
                    "symbol": symbol,
                    "asset_class": "option" if is_option else "equity",
                    "qty": qty,
                    "side": "long" if qty >= 0 else "short",
                    "avg_entry_price": round(avg_entry, 4),
                    "current_price": price or 0.0,
                    "market_value": round(market_value, 2),
                    "cost_basis": cost_basis,
                    "unrealized_pl": round(unrealized, 2),
                    "unrealized_plpc": round(unrealized / cost_basis * 100.0, 2) if cost_basis else 0.0,
                })
            return out

        return await self.cache.get_or_fetch("tradier:positions", self._ttls["positions"], _fetch)

    async def clock(self) -> Fetched:
        async def _fetch() -> Dict[str, Any]:
            raw = await self._get("/v1/markets/clock")
            clock = raw.get("clock") or {}
            return {
                "is_open": (clock.get("state") == "open"),
                "next_open": None,
                "next_close": None,
                "timestamp": clock.get("timestamp"),
                "state": clock.get("state"),
            }

        return await self.cache.get_or_fetch("tradier:clock", self._ttls["clock"], _fetch)

    # ----------------------------------------------------------- market data

    async def _quotes(self, symbols: List[str], greeks: bool = False) -> List[Dict[str, Any]]:
        raw = await self._get("/v1/markets/quotes", {
            "symbols": ",".join(symbols), "greeks": "true" if greeks else "false",
        })
        return listify((raw.get("quotes") or {}).get("quote"))

    async def batch_quotes(self, symbols: List[str]) -> Fetched:
        """Quotes for many symbols for the scan prefilter. Tradier accepts
        large symbol lists per call, so this is cheap even for thousands of
        names. Returns {symbol: {price, change_pct, volume, week_52_high}}."""
        joined_key = f"tradier:batch:{len(symbols)}:{symbols[0] if symbols else ''}"

        async def _fetch() -> Dict[str, Dict[str, Any]]:
            out: Dict[str, Dict[str, Any]] = {}
            chunk = 250  # keep the symbols= query string well under URL limits
            for i in range(0, len(symbols), chunk):
                quotes = await self._quotes(symbols[i:i + chunk])
                for q in quotes:
                    sym = (q.get("symbol") or "").upper()
                    if not sym:
                        continue
                    out[sym] = {
                        "symbol": sym,
                        "price": _f(q.get("last")),
                        "change_pct": _f(q.get("change_percentage")),
                        "volume": _f(q.get("volume")),
                        "ma50": None,
                        "ma200": None,
                        "week_52_high": _f(q.get("week_52_high")),
                    }
            if not out:
                raise ProviderError("tradier: batch quotes returned no rows")
            return out

        return await self.cache.get_or_fetch(joined_key, self._ttls["stock_snapshot"], _fetch)

    async def stock_snapshot(self, symbol: str) -> Fetched:
        async def _fetch() -> Dict[str, Any]:
            quotes = await self._quotes([symbol])
            if not quotes:
                raise ProviderError(f"tradier: no quote returned for {symbol}")
            q = quotes[0]
            return {
                "symbol": symbol,
                "price": _f(q.get("last")),
                "prev_close": _f(q.get("prevclose")),
                "change_pct": _f(q.get("change_percentage")),
                "day_volume": int(_f(q.get("volume")) or 0),
                "ts": q.get("trade_date"),
            }

        return await self.cache.get_or_fetch(
            f"tradier:snapshot:{symbol}", self._ttls["stock_snapshot"], _fetch
        )

    async def bars(self, symbol: str, days: int = 120) -> Fetched:
        async def _fetch() -> List[Dict[str, Any]]:
            start = (dt.date.today() - dt.timedelta(days=int(days * 1.7) + 5)).isoformat()
            end = dt.date.today().isoformat()
            raw = await self._get("/v1/markets/history", {
                "symbol": symbol, "interval": "daily", "start": start, "end": end,
            })
            days_list = listify((raw.get("history") or {}).get("day"))
            out = [
                {"date": d.get("date"), "close": _f(d.get("close"))}
                for d in days_list if _f(d.get("close")) is not None
            ]
            if not out:
                raise ProviderError(f"tradier: no history returned for {symbol}")
            out.sort(key=lambda r: r["date"])
            return out[-days:]

        return await self.cache.get_or_fetch(
            f"tradier:bars:{symbol}:{days}", self._ttls["bars"], _fetch
        )

    async def option_expirations(self, underlying: str) -> Fetched:
        async def _fetch() -> List[str]:
            raw = await self._get("/v1/markets/options/expirations", {
                "symbol": underlying, "includeAllRoots": "true", "strikes": "false",
            })
            exps = (raw.get("expirations") or {})
            return sorted(listify(exps.get("date")))

        return await self.cache.get_or_fetch(
            f"tradier:expirations:{underlying}", self._ttls["history"], _fetch
        )

    async def chain(
        self,
        underlying: str,
        type_: str = "call",
        exp_gte: Optional[str] = None,
        exp_lte: Optional[str] = None,
        strike_gte: Optional[float] = None,
        strike_lte: Optional[float] = None,
    ) -> Fetched:
        """Merged chain across expirations in the window (one Tradier chain
        request per expiration), filtered to the requested side and strikes."""
        key = (f"tradier:chain:{underlying}:{type_}:{exp_gte}:{exp_lte}:"
               f"{strike_gte}:{strike_lte}")

        async def _fetch() -> List[Dict[str, Any]]:
            expirations = (await self.option_expirations(underlying)).data
            selected = [
                e for e in expirations
                if (not exp_gte or e >= exp_gte) and (not exp_lte or e <= exp_lte)
            ]
            today = dt.date.today()
            out: List[Dict[str, Any]] = []
            for expiration in selected:
                raw = await self._get("/v1/markets/options/chains", {
                    "symbol": underlying, "expiration": expiration, "greeks": "true",
                })
                for c in listify((raw.get("options") or {}).get("option")):
                    if (c.get("option_type") or "").lower() != type_:
                        continue
                    norm = norm_chain_item(c, underlying, today)
                    if strike_gte is not None and norm["strike"] < strike_gte:
                        continue
                    if strike_lte is not None and norm["strike"] > strike_lte:
                        continue
                    out.append(norm)
            return out

        return await self.cache.get_or_fetch(key, self._ttls["chain"], _fetch)

    async def option_latest_quote(self, occ_symbol: str) -> Dict[str, Any]:
        occ = occ_symbol.replace(" ", "").upper()
        quotes = await self._quotes([occ], greeks=False)
        q = quotes[0] if quotes else {}
        bid = _f(q.get("bid")) or 0.0
        ask = _f(q.get("ask")) or 0.0
        mid = round((bid + ask) / 2.0, 4) if bid > 0 and ask > 0 else None
        return {"occ_symbol": occ, "bid": bid, "ask": ask, "mid": mid,
                "ts": q.get("trade_date")}

    # ---------------------------------------------------------------- orders

    async def submit_order(self, occ_symbol: str, qty: int, limit_price: float,
                           side: str = "buy") -> Dict[str, Any]:
        """Submit a single-leg option day limit order. Real money on
        production - callers MUST have passed the live confirm gates."""
        acct_id = await self._account_id()
        occ = occ_symbol.replace(" ", "").upper()
        underlying = occ_underlying(occ)
        if not underlying:
            raise ProviderError(f"tradier: cannot parse underlying from {occ}")
        tradier_side = "buy_to_open" if side.lower() == "buy" else "sell_to_close"
        body = {
            "class": "option",
            "symbol": underlying,
            "option_symbol": occ,
            "side": tradier_side,
            "quantity": str(int(qty)),
            "type": "limit",
            "duration": "day",
            "price": f"{float(limit_price):.2f}",
        }
        log.info("submitting %s order via tradier (%s): %s x%s limit %s",
                 tradier_side, self.environment, occ, qty, body["price"])
        raw = await self._post_form(f"/v1/accounts/{acct_id}/orders", body)
        order = raw.get("order") or {}
        if (order.get("status") or "").lower() not in ("ok", "open", "pending", "filled"):
            raise ProviderError(f"tradier: order not accepted: {raw}")
        return {
            "id": str(order.get("id") or ""),
            "status": "new",
            "symbol": occ,
            "qty": str(int(qty)),
            "filled_qty": None,
            "filled_avg_price": None,
            "limit_price": body["price"],
            "side": side,
            "type": "limit",
            "time_in_force": "day",
            "submitted_at": dt.datetime.now().isoformat(timespec="seconds"),
        }

    async def get_order(self, order_id: str) -> Dict[str, Any]:
        acct_id = await self._account_id()
        raw = await self._get(f"/v1/accounts/{acct_id}/orders/{order_id}")
        o = raw.get("order") or {}
        return {
            "id": str(o.get("id") or order_id),
            "status": map_order_status(o.get("status")),
            "symbol": (o.get("option_symbol") or o.get("symbol") or "").upper(),
            "qty": o.get("quantity"),
            "filled_qty": o.get("exec_quantity"),
            "filled_avg_price": o.get("avg_fill_price"),
            "limit_price": o.get("price"),
            "side": o.get("side"),
            "type": o.get("type"),
            "time_in_force": o.get("duration"),
            "submitted_at": o.get("create_date"),
            "reject_reason": o.get("reason_description"),
        }

    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        acct_id = await self._account_id()
        await self._request_json(
            "DELETE", f"{self.base}/v1/accounts/{acct_id}/orders/{order_id}",
            headers=self._headers(),
        )
        return {"id": order_id, "status": "pending_cancel"}
