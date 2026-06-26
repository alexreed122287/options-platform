"""Market-facing routes: health, regime, watchlist, segments."""
import datetime as dt
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

from api import db
from api.deps import get_deps
from data.base import ProviderError
from data.env import env_bool

router = APIRouter()


@router.get("/health")
async def health() -> Dict[str, Any]:
    deps = get_deps()
    from api.app import APP_VERSION
    return {
        "version": APP_VERSION,
        "broker": deps.broker_name,
        "data_source": deps.data_source_name,
        "mode": "paper" if deps.broker.paper else "LIVE",
        "live_trading_enabled": env_bool("LIVE_TRADING_ENABLED", False),
        "scan_loop": deps.alerts.status(),
        "providers": {
            "fmp": {**deps.fmp.status(), "rate": deps.fmp.budget.snapshot()},
            "alpaca": {
                **deps.alpaca.status(),
                "rate_trading": deps.alpaca.budget.snapshot(),
                "rate_data": deps.alpaca.budget_data.snapshot(),
            },
            "public": {**deps.public.status(), "rate": deps.public.budget.snapshot()},
            "tradier": {
                **deps.tradier.status(),
                "environment": deps.tradier.environment,
                "rate": deps.tradier.budget.snapshot(),
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
async def recommendations(
    refresh: bool = Query(False),
    sector: List[str] = Query(default=[]),
    theme: List[str] = Query(default=[]),
    dte: Optional[str] = Query(None),
    price_min: Optional[float] = Query(None),
    price_max: Optional[float] = Query(None),
) -> Dict[str, Any]:
    deps = get_deps()
    segs = deps.config.get("segments")
    # validate against known segments so a typo can't silently scan nothing
    known_sectors = set((segs.get("sector_of") or {}).values())
    for sec in sector:
        if sec and sec not in known_sectors:
            raise HTTPException(status_code=400, detail=f"unknown sector: {sec}")
    known_themes = segs.get("themes") or {}
    for th in theme:
        if th and th not in known_themes:
            raise HTTPException(status_code=400, detail=f"unknown theme: {th}")
    if dte:
        valid = {o["key"] for o in deps.config.get("scoring").get("dte_presets", {}).get("options", [])}
        if dte not in valid:
            raise HTTPException(status_code=400, detail=f"unknown dte preset: {dte}")
    if price_min is not None and price_min < 0:
        raise HTTPException(status_code=400, detail="price_min must be >= 0")
    if price_max is not None and price_max < 0:
        raise HTTPException(status_code=400, detail="price_max must be >= 0")
    if price_min is not None and price_max is not None and price_min > price_max:
        raise HTTPException(status_code=400, detail="price_min must be <= price_max")
    active_sectors = [s for s in sector if s] or None
    active_themes = [t for t in theme if t] or None
    return await deps.scanner.scan(
        refresh=refresh, sectors=active_sectors, themes=active_themes, dte=dte or None,
        price_min=price_min, price_max=price_max,
    )


@router.get("/segments")
async def segments() -> Dict[str, Any]:
    """Scan filter options: sectors and themes (with ticker counts) plus DTE presets."""
    deps = get_deps()
    segs = deps.config.get("segments")
    sector_of = segs.get("sector_of") or {}
    sector_counts: Dict[str, int] = {}
    for sec in sector_of.values():
        sector_counts[sec] = sector_counts.get(sec, 0) + 1
    sectors = [{"name": s, "count": n}
               for s, n in sorted(sector_counts.items(), key=lambda kv: -kv[1])]
    themes = [{"name": t, "count": len(members)}
              for t, members in sorted((segs.get("themes") or {}).items())]
    dte_presets = deps.config.get("scoring").get("dte_presets", {}).get("options", [])
    return {"sectors": sectors, "themes": themes, "dte_presets": dte_presets}


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
