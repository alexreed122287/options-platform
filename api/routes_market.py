"""Market-facing routes: health, regime, watchlist."""
import datetime as dt
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query

from api import db
from api.deps import get_deps
from data.base import ProviderError
from data.env import env_bool

router = APIRouter()


@router.get("/health")
async def health() -> Dict[str, Any]:
    deps = get_deps()
    return {
        "mode": "paper" if deps.alpaca.paper else "LIVE",
        "live_trading_enabled": env_bool("LIVE_TRADING_ENABLED", False),
        "providers": {
            "fmp": {**deps.fmp.status(), "rate": deps.fmp.budget.snapshot()},
            "alpaca": {
                **deps.alpaca.status(),
                "rate_trading": deps.alpaca.budget.snapshot(),
                "rate_data": deps.alpaca.budget_data.snapshot(),
            },
        },
        "cache": deps.cache.stats(),
    }


@router.get("/regime")
async def regime(refresh: bool = Query(False)) -> Dict[str, Any]:
    return await get_deps().regime.compute(refresh=refresh)


@router.get("/regime/history")
async def regime_history(limit: int = Query(30, ge=1, le=365)):
    return get_deps().regime.history(limit)


@router.get("/recommendations")
async def recommendations(refresh: bool = Query(False)) -> Dict[str, Any]:
    return await get_deps().scanner.scan(refresh=refresh)


@router.get("/universe")
async def universe() -> Dict[str, Any]:
    deps = get_deps()
    return {
        "tickers": deps.config.get("universe")["tickers"],
        "scoring": deps.config.get("scoring"),
    }


@router.get("/watchlist")
async def watchlist() -> Dict[str, Any]:
    deps = get_deps()
    rows = db.query("SELECT symbol, added_at FROM watchlist ORDER BY symbol")
    quotes: Dict[str, Any] = {}
    stale = False
    error = None
    if rows:
        try:
            fetched = await deps.fmp.batch_quotes([r["symbol"] for r in rows])
            quotes = fetched.data
            stale = fetched.stale
        except ProviderError as exc:
            error = str(exc)
    items = []
    for row in rows:
        q = quotes.get(row["symbol"], {})
        items.append({
            "symbol": row["symbol"],
            "added_at": row["added_at"],
            "price": q.get("price"),
            "change_pct": q.get("change_pct"),
            "above_ma50": (
                q["price"] > q["ma50"]
                if q.get("price") is not None and q.get("ma50") is not None else None
            ),
            "above_ma200": (
                q["price"] > q["ma200"]
                if q.get("price") is not None and q.get("ma200") is not None else None
            ),
        })
    return {"items": items, "stale": stale, "error": error}


@router.post("/watchlist/{symbol}")
async def watchlist_add(symbol: str) -> Dict[str, Any]:
    symbol = symbol.strip().upper()
    if not symbol.isalnum() or len(symbol) > 10:
        raise HTTPException(status_code=400, detail="invalid symbol")
    db.execute(
        "INSERT OR IGNORE INTO watchlist (symbol, added_at) VALUES (?, ?)",
        (symbol, dt.datetime.now().isoformat(timespec="seconds")),
    )
    return {"ok": True, "symbol": symbol}


@router.delete("/watchlist/{symbol}")
async def watchlist_remove(symbol: str) -> Dict[str, Any]:
    db.execute("DELETE FROM watchlist WHERE symbol = ?", (symbol.strip().upper(),))
    return {"ok": True}
