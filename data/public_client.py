"""Public.com client.

Auth: a long-lived API secret (PUBLIC_API_SECRET, generated at public.com
Settings -> Security -> API) is exchanged for a short-lived JWT, which is
cached in memory and refreshed automatically before expiry. The secret and
every issued token are registered with the log redactor.

IMPORTANT: Public has NO paper environment - every order placed through this
client is a real-money order. The API routes therefore apply the full live
gating (LIVE_TRADING_ENABLED env flag + typed LIVE acknowledgment) whenever
BROKER=public, regardless of ALPACA_PAPER.

Endpoints (docs: https://public.com/api/docs):
  POST   /userapiauthservice/personal/access-tokens        secret -> JWT
  GET    /userapigateway/trading/account                    account id, options level
  GET    /userapigateway/trading/{id}/portfolio/v2          equity, positions
  POST   /userapigateway/marketdata/{id}/quotes             equity + option quotes
  POST   /userapigateway/marketdata/{id}/option-expirations expiry list
  POST   /userapigateway/marketdata/{id}/option-chain       per-expiry, with greeks
  POST   /userapigateway/trading/{id}/order                 place (async, client uuid)
  GET    /userapigateway/trading/{id}/order/{orderId}       status
  DELETE /userapigateway/trading/{id}/order/{orderId}       cancel
"""
import asyncio
import datetime as dt
import logging
import re
import time
import uuid
from typing import Any, Dict, List, Optional

from .base import BaseClient, ProviderError, register_secret
from .cache import Fetched, RateBudget, TTLCache
from .env import env, secret

log = logging.getLogger("data.public")

BASE = "https://api.public.com"

ORDER_STATUS_MAP = {
    "NEW": "new",
    "PARTIALLY_FILLED": "partially_filled",
    "FILLED": "filled",
    "CANCELLED": "canceled",
    "QUEUED_CANCELLED": "canceled",
    "PENDING_CANCEL": "pending_cancel",
    "PENDING_REPLACE": "pending_replace",
    "REPLACED": "replaced",
    "REJECTED": "rejected",
    "EXPIRED": "expired",
}

_OCC_PARSE = re.compile(r"^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


def map_order_status(status: Optional[str]) -> Optional[str]:
    if not status:
        return None
    return ORDER_STATUS_MAP.get(status.upper(), status.lower())


def compact_occ(symbol: Optional[str]) -> str:
    """Normalize OSI/OCC symbols ('AAPL  250620C00200000') to the compact
    form used across the platform ('AAPL250620C00200000')."""
    return (symbol or "").replace(" ", "").upper()


def _fnum(value: Any) -> Optional[float]:
    """Public returns numerics as strings; '' and absent both mean unknown."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.replace(",", "").strip()
        if not value:
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def norm_chain_item(item: Dict[str, Any], underlying: str, type_: str,
                    today: dt.date) -> Optional[Dict[str, Any]]:
    """Normalize one option-chain entry to the platform contract shape
    (same keys AlpacaClient.chain produces). Returns None if the OCC symbol
    cannot be parsed."""
    inst = item.get("instrument") or {}
    occ = compact_occ(inst.get("symbol"))
    match = _OCC_PARSE.match(occ)
    if not match:
        return None
    expiration = f"20{match.group(2)}-{match.group(3)}-{match.group(4)}"
    details = item.get("optionDetails") or {}
    greeks = details.get("greeks") or {}
    strike = _fnum(details.get("strikePrice"))
    if strike is None:
        strike = int(match.group(6)) / 1000.0
    bid = _fnum(item.get("bid")) or 0.0
    ask = _fnum(item.get("ask")) or 0.0
    mid = _fnum(details.get("midPrice"))
    if (mid is None or mid <= 0) and bid > 0 and ask > 0:
        mid = round((bid + ask) / 2.0, 4)
    if mid is not None and mid <= 0:
        mid = None
    try:
        dte = (dt.date.fromisoformat(expiration) - today).days
    except ValueError:
        dte = None
    return {
        "occ_symbol": occ,
        "underlying": underlying,
        "type": type_,
        "strike": strike,
        "expiration": expiration,
        "dte": dte,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "last": _fnum(item.get("last")),
        "volume": int(_fnum(item.get("volume")) or 0),
        "open_interest": int(_fnum(item.get("openInterest")) or 0),
        "iv": _fnum(greeks.get("impliedVolatility")),
        "delta": _fnum(greeks.get("delta")),
        "gamma": _fnum(greeks.get("gamma")),
        "theta": _fnum(greeks.get("theta")),
        "vega": _fnum(greeks.get("vega")),
    }


class PublicClient(BaseClient):
    name = "public"

    def __init__(self, cache: TTLCache, budget: RateBudget,
                 ttls: Dict[str, float], cfg: Dict[str, Any]):
        super().__init__(cache, budget)
        self._ttls = ttls
        self._cfg = cfg or {}
        self._token: Optional[str] = None
        self._token_expiry = 0.0
        self._token_lock = asyncio.Lock()
        self._account_id_cached: Optional[str] = None
        self._account_meta: Dict[str, Any] = {}

    @property
    def configured(self) -> bool:
        return bool(secret("PUBLIC_API_SECRET"))

    @property
    def paper(self) -> bool:
        """Public has no paper environment - always real money."""
        return False

    # ----------------------------------------------------------------- auth

    async def _ensure_token(self) -> str:
        async with self._token_lock:
            now = time.time()
            if self._token and now < self._token_expiry - 300:
                return self._token
            if not self.configured:
                raise ProviderError("public: PUBLIC_API_SECRET not set in .env")
            validity = max(5, min(1440, int(self._cfg.get("token_validity_minutes", 60))))
            resp = await self._request_json(
                "POST", f"{BASE}/userapiauthservice/personal/access-tokens",
                json_body={"validityInMinutes": validity,
                           "secret": secret("PUBLIC_API_SECRET")},
            )
            token = resp.get("accessToken") if isinstance(resp, dict) else None
            if not token:
                raise ProviderError("public: token exchange returned no accessToken")
            register_secret(token)
            self._token = token
            self._token_expiry = now + validity * 60.0
            log.info("public: access token refreshed (valid %s minutes)", validity)
            return token

    def _invalidate_token(self) -> None:
        self._token = None
        self._token_expiry = 0.0

    async def _authed(self, method: str, path: str,
                      json_body: Optional[Dict[str, Any]] = None) -> Any:
        """Authenticated request with a single retry on 401 (expired/revoked
        token is re-exchanged once)."""
        for attempt in (1, 2):
            token = await self._ensure_token()
            try:
                return await self._request_json(
                    method, f"{BASE}{path}",
                    headers={"Authorization": f"Bearer {token}"},
                    json_body=json_body,
                )
            except ProviderError as exc:
                if "HTTP 401" in str(exc) and attempt == 1:
                    self._invalidate_token()
                    continue
                raise
        raise ProviderError("public: request failed after token retry")

    # -------------------------------------------------------------- account

    async def _account_id(self) -> str:
        override = env("PUBLIC_ACCOUNT_ID")
        if override:
            return override
        if self._account_id_cached:
            return self._account_id_cached
        raw = await self._authed("GET", "/userapigateway/trading/account")
        accounts = raw.get("accounts") or []
        brokerage = next(
            (a for a in accounts if a.get("accountType") == "BROKERAGE"),
            accounts[0] if accounts else None,
        )
        if not brokerage or not brokerage.get("accountId"):
            raise ProviderError("public: no brokerage account found for this secret")
        self._account_id_cached = brokerage["accountId"]
        self._account_meta = brokerage
        return self._account_id_cached

    async def _portfolio(self) -> Fetched:
        async def _fetch() -> Dict[str, Any]:
            acct_id = await self._account_id()
            return await self._authed(
                "GET", f"/userapigateway/trading/{acct_id}/portfolio/v2"
            )

        return await self.cache.get_or_fetch(
            "public:portfolio", self._ttls["positions"], _fetch
        )

    async def account(self) -> Fetched:
        fetched = await self._portfolio()
        port = fetched.data
        buying_power = port.get("buyingPower") or {}
        equity_total = sum(
            _fnum(bucket.get("value")) or 0.0 for bucket in port.get("equity") or []
        )
        meta = self._account_meta or {}
        data = {
            "status": meta.get("tradePermissions") or "ACTIVE",
            "currency": "USD",
            "equity": round(equity_total, 2),
            "last_equity": None,
            "cash": _fnum(buying_power.get("cashOnlyBuyingPower")),
            "buying_power": _fnum(buying_power.get("buyingPower")) or 0.0,
            "options_buying_power": _fnum(buying_power.get("optionsBuyingPower")),
            "options_approved_level": meta.get("optionsLevel"),
            "paper": False,
            "broker": "public",
        }
        return Fetched(data, stale=fetched.stale, as_of=fetched.as_of,
                       error=fetched.error)

    async def positions(self) -> Fetched:
        fetched = await self._portfolio()
        out: List[Dict[str, Any]] = []
        for p in fetched.data.get("positions") or []:
            inst = p.get("instrument") or {}
            cost = p.get("costBasis") or {}
            qty = _fnum(p.get("quantity")) or 0.0
            symbol = inst.get("symbol") or ""
            if (inst.get("type") or "").upper() == "OPTION":
                symbol = compact_occ(symbol)
            out.append({
                "symbol": symbol,
                "asset_class": (inst.get("type") or "").lower() or None,
                "qty": qty,
                "side": "long" if qty >= 0 else "short",
                "avg_entry_price": _fnum(cost.get("unitCost")) or 0.0,
                "current_price": _fnum((p.get("lastPrice") or {}).get("lastPrice")) or 0.0,
                "market_value": _fnum(p.get("currentValue")) or 0.0,
                "cost_basis": _fnum(cost.get("totalCost")) or 0.0,
                "unrealized_pl": _fnum(cost.get("gainValue")) or 0.0,
                "unrealized_plpc": _fnum(cost.get("gainPercentage")) or 0.0,
            })
        return Fetched(out, stale=fetched.stale, as_of=fetched.as_of,
                       error=fetched.error)

    # ----------------------------------------------------------- market data

    async def _quotes(self, instruments: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        acct_id = await self._account_id()
        raw = await self._authed(
            "POST", f"/userapigateway/marketdata/{acct_id}/quotes",
            json_body={"instruments": instruments},
        )
        return raw.get("quotes") or []

    async def stock_snapshot(self, symbol: str) -> Fetched:
        async def _fetch() -> Dict[str, Any]:
            quotes = await self._quotes([{"symbol": symbol, "type": "EQUITY"}])
            if not quotes:
                raise ProviderError(f"public: no quote returned for {symbol}")
            q = quotes[0]
            price = _fnum(q.get("last"))
            prev_close = _fnum(q.get("previousClose"))
            change_pct = _fnum((q.get("oneDayChange") or {}).get("percentChange"))
            if change_pct is None and price and prev_close:
                change_pct = round((price / prev_close - 1.0) * 100.0, 2)
            return {
                "symbol": symbol,
                "price": price,
                "prev_close": prev_close,
                "change_pct": change_pct,
                "day_volume": int(_fnum(q.get("volume")) or 0),
                "ts": q.get("lastTimestamp"),
            }

        return await self.cache.get_or_fetch(
            f"public:snapshot:{symbol}", self._ttls["stock_snapshot"], _fetch
        )

    async def bars(self, symbol: str, days: int = 120) -> Fetched:
        """Public's API has no historical bars endpoint; the scanner falls
        back to FMP history for trend computation."""
        raise ProviderError("public: historical bars not available - FMP history is used instead")

    async def option_expirations(self, underlying: str) -> Fetched:
        async def _fetch() -> List[str]:
            acct_id = await self._account_id()
            raw = await self._authed(
                "POST", f"/userapigateway/marketdata/{acct_id}/option-expirations",
                json_body={"instrument": {"symbol": underlying, "type": "EQUITY"}},
            )
            return sorted(raw.get("expirations") or [])

        return await self.cache.get_or_fetch(
            f"public:expirations:{underlying}", self._ttls["history"], _fetch
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
        """Merged chain across all expirations inside the window, normalized
        to the platform contract shape (greeks and IV included by Public)."""
        key = (
            f"public:chain:{underlying}:{type_}:{exp_gte}:{exp_lte}:"
            f"{strike_gte}:{strike_lte}"
        )

        async def _fetch() -> List[Dict[str, Any]]:
            acct_id = await self._account_id()
            expirations = (await self.option_expirations(underlying)).data
            selected = [
                e for e in expirations
                if (not exp_gte or e >= exp_gte) and (not exp_lte or e <= exp_lte)
            ]
            max_exp = int(self._cfg.get("max_expirations_per_chain", 6))
            if len(selected) > max_exp:
                log.info("public: %s has %d expirations in window, fetching first %d",
                         underlying, len(selected), max_exp)
                selected = selected[:max_exp]
            side_key = "calls" if type_ == "call" else "puts"
            today = dt.date.today()
            out: List[Dict[str, Any]] = []
            for expiration in selected:
                raw = await self._authed(
                    "POST", f"/userapigateway/marketdata/{acct_id}/option-chain",
                    json_body={
                        "instrument": {"symbol": underlying, "type": "EQUITY"},
                        "expirationDate": expiration,
                    },
                )
                for item in raw.get(side_key) or []:
                    norm = norm_chain_item(item, underlying, type_, today)
                    if norm is None:
                        continue
                    if strike_gte is not None and norm["strike"] < strike_gte:
                        continue
                    if strike_lte is not None and norm["strike"] > strike_lte:
                        continue
                    out.append(norm)
            return out

        return await self.cache.get_or_fetch(key, self._ttls["chain"], _fetch)

    async def option_latest_quote(self, occ_symbol: str) -> Dict[str, Any]:
        """Uncached latest quote for one contract (used by order preview)."""
        occ = compact_occ(occ_symbol)
        quotes = await self._quotes([{"symbol": occ, "type": "OPTION"}])
        q = quotes[0] if quotes else {}
        bid = _fnum(q.get("bid")) or 0.0
        ask = _fnum(q.get("ask")) or 0.0
        mid = _fnum((q.get("optionDetails") or {}).get("midPrice"))
        if (mid is None or mid <= 0) and bid > 0 and ask > 0:
            mid = round((bid + ask) / 2.0, 4)
        if mid is not None and mid <= 0:
            mid = None
        return {"occ_symbol": occ, "bid": bid, "ask": ask, "mid": mid,
                "ts": q.get("lastTimestamp")}

    # ---------------------------------------------------------------- orders

    async def submit_order(self, occ_symbol: str, qty: int, limit_price: float,
                           side: str = "buy") -> Dict[str, Any]:
        """Submit a day limit order. REAL MONEY - callers MUST have passed the
        live confirm gates. Order placement is asynchronous at Public; the
        response confirms submission, not execution."""
        acct_id = await self._account_id()
        occ = compact_occ(occ_symbol)
        order_id = str(uuid.uuid4())
        body = {
            "orderId": order_id,
            "instrument": {"symbol": occ, "type": "OPTION"},
            "orderSide": side.upper(),
            "orderType": "LIMIT",
            "expiration": {"timeInForce": "DAY"},
            "quantity": str(int(qty)),
            "limitPrice": f"{float(limit_price):.2f}",
            "openCloseIndicator": "OPEN" if side.lower() == "buy" else "CLOSE",
        }
        log.info("submitting %s order via public: %s x%s limit %s (REAL MONEY)",
                 side, occ, qty, body["limitPrice"])
        raw = await self._authed(
            "POST", f"/userapigateway/trading/{acct_id}/order", json_body=body
        )
        return {
            "id": (raw or {}).get("orderId") or order_id,
            "status": "new",
            "symbol": occ,
            "qty": str(int(qty)),
            "filled_qty": None,
            "filled_avg_price": None,
            "limit_price": body["limitPrice"],
            "side": side,
            "type": "limit",
            "time_in_force": "day",
            "submitted_at": dt.datetime.now().isoformat(timespec="seconds"),
        }

    async def get_order(self, order_id: str) -> Dict[str, Any]:
        acct_id = await self._account_id()
        raw = await self._authed(
            "GET", f"/userapigateway/trading/{acct_id}/order/{order_id}"
        )
        return {
            "id": raw.get("orderId"),
            "status": map_order_status(raw.get("status")),
            "symbol": compact_occ((raw.get("instrument") or {}).get("symbol")),
            "qty": raw.get("quantity"),
            "filled_qty": raw.get("filledQuantity"),
            "filled_avg_price": raw.get("averagePrice"),
            "limit_price": raw.get("limitPrice"),
            "side": (raw.get("side") or "").lower(),
            "type": (raw.get("type") or "").lower(),
            "time_in_force": ((raw.get("expiration") or {}).get("timeInForce") or "").lower(),
            "submitted_at": raw.get("createdAt"),
            "reject_reason": raw.get("rejectReason"),
        }

    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        acct_id = await self._account_id()
        await self._authed(
            "DELETE", f"/userapigateway/trading/{acct_id}/order/{order_id}"
        )
        return {"id": order_id, "status": "pending_cancel"}
