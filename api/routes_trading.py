"""Trading routes: account, positions, order preview/submit, journal, alerts.

The active broker (deps.broker) is Alpaca or Public per the BROKER env var.
Both expose the same normalized interface: account(), positions(),
option_latest_quote(), submit_order(), get_order(), and a .paper property.
Public's .paper is always False - Public has no paper environment.

SAFETY INVARIANTS (do not weaken):
- POST /orders requires confirmed=true - the UI confirmation step. This
  applies in paper mode too. No other code path submits orders.
- Live mode (Alpaca with ALPACA_PAPER=false, or Public always) additionally
  requires BOTH the LIVE_TRADING_ENABLED env flag and live_ack == "LIVE"
  typed in the UI.
"""
import datetime as dt
import json
import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from api import db
from api.deps import get_deps
from data.base import ProviderError
from data.env import env_bool

router = APIRouter()

OCC_RE = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")


def _occ_underlying(occ: str) -> Optional[str]:
    match = re.match(r"^([A-Z]{1,6})\d{6}", occ)
    return match.group(1) if match else None


def _trim_order(order: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": order.get("id"),
        "status": order.get("status"),
        "symbol": order.get("symbol"),
        "qty": order.get("qty"),
        "filled_qty": order.get("filled_qty"),
        "filled_avg_price": order.get("filled_avg_price"),
        "limit_price": order.get("limit_price"),
        "side": order.get("side"),
        "type": order.get("type"),
        "time_in_force": order.get("time_in_force"),
        "submitted_at": order.get("submitted_at"),
        "reject_reason": order.get("reject_reason"),
    }


class OrderPreviewRequest(BaseModel):
    occ_symbol: str
    limit_price: Optional[float] = Field(None, gt=0)


class OrderSubmitRequest(BaseModel):
    occ_symbol: str
    qty: int = Field(..., ge=1)
    limit_price: float = Field(..., gt=0)
    confirmed: bool = False
    live_ack: Optional[str] = None
    allow_above_ask: bool = False


class NotesRequest(BaseModel):
    notes: str = ""


class CloseRequest(BaseModel):
    exit_price: float = Field(..., ge=0)


# ------------------------------------------------------- account/positions

@router.get("/account")
async def account() -> Dict[str, Any]:
    deps = get_deps()
    try:
        fetched = await deps.broker.account()
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"account": fetched.data, "broker": deps.broker_name,
            "stale": fetched.stale, "as_of": fetched.as_of_iso}


@router.get("/positions")
async def positions() -> Dict[str, Any]:
    deps = get_deps()
    try:
        fetched = await deps.broker.positions()
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"positions": fetched.data, "broker": deps.broker_name,
            "stale": fetched.stale, "as_of": fetched.as_of_iso}


# ----------------------------------------------------------------- orders

@router.post("/orders/preview")
async def order_preview(req: OrderPreviewRequest) -> Dict[str, Any]:
    deps = get_deps()
    occ = req.occ_symbol.strip().upper().replace(" ", "")
    if not OCC_RE.match(occ):
        raise HTTPException(status_code=400, detail="invalid OCC symbol")
    risk = deps.config.get("settings")["risk"]
    try:
        quote = await deps.broker.option_latest_quote(occ)
        acct = await deps.broker.account()
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    equity = acct.data["equity"]
    price = req.limit_price or quote["mid"]
    max_risk = equity * float(risk["max_risk_pct_equity"]) / 100.0
    suggested_qty = 0
    if price and price > 0:
        suggested_qty = int(max_risk // (price * 100.0))
    suggested_qty = max(0, min(suggested_qty, int(risk["max_contracts_per_order"])))

    warnings = []
    if suggested_qty == 0:
        warnings.append(
            "a single contract at this price exceeds the configured risk budget "
            f"({risk['max_risk_pct_equity']}% of equity = ${max_risk:,.0f})"
        )
    if quote["mid"] is None:
        warnings.append("no live bid/ask for this contract right now")

    cached = deps.scanner.find_cached(occ)
    live_mode = not deps.broker.paper
    if deps.broker_name == "public":
        warnings.append("Public has no paper environment - this order would use real money")
    return {
        "occ_symbol": occ,
        "underlying": _occ_underlying(occ),
        "broker": deps.broker_name,
        "bid": quote["bid"],
        "ask": quote["ask"],
        "mid": quote["mid"],
        "limit_price": round(price, 2) if price else None,
        "equity": equity,
        "stale_account": acct.stale,
        "max_risk_pct_equity": risk["max_risk_pct_equity"],
        "max_risk_dollars": round(max_risk, 2),
        "suggested_qty": suggested_qty,
        "max_contracts_per_order": risk["max_contracts_per_order"],
        "est_max_loss": round((price or 0.0) * 100.0 * max(suggested_qty, 1), 2),
        "score_snapshot": (
            {"score": cached.get("score"), "components": cached.get("components")}
            if cached else None
        ),
        "mode": "LIVE" if live_mode else "paper",
        "live_blocked_by_env": live_mode and not env_bool("LIVE_TRADING_ENABLED", False),
        "warnings": warnings,
    }


@router.post("/orders")
async def order_submit(req: OrderSubmitRequest) -> Dict[str, Any]:
    deps = get_deps()
    occ = req.occ_symbol.strip().upper().replace(" ", "")
    if not OCC_RE.match(occ):
        raise HTTPException(status_code=400, detail="invalid OCC symbol")

    # UI confirmation gate - applies to paper mode too. Never weaken.
    if req.confirmed is not True:
        raise HTTPException(
            status_code=400,
            detail="order not confirmed - the UI confirmation step is required",
        )

    risk = deps.config.get("settings")["risk"]
    max_qty = int(risk["max_contracts_per_order"])
    if req.qty > max_qty:
        raise HTTPException(
            status_code=400,
            detail=f"qty {req.qty} exceeds max_contracts_per_order ({max_qty})",
        )

    # Public is always real money; Alpaca is live when ALPACA_PAPER=false.
    live_mode = not deps.broker.paper
    if live_mode:
        if not env_bool("LIVE_TRADING_ENABLED", False):
            raise HTTPException(
                status_code=403,
                detail=f"live mode blocked ({deps.broker_name} is real money): "
                       "set LIVE_TRADING_ENABLED=true in .env to allow live orders",
            )
        if (req.live_ack or "").strip() != "LIVE":
            raise HTTPException(
                status_code=403,
                detail="live mode blocked: type LIVE in the confirmation box",
            )

    # Fat-finger guard: block limits far above the current ask unless overridden.
    quote = None
    try:
        quote = await deps.broker.option_latest_quote(occ)
    except ProviderError:
        pass
    if (
        quote and quote.get("ask")
        and req.limit_price > quote["ask"] * 1.25
        and not req.allow_above_ask
    ):
        raise HTTPException(
            status_code=400,
            detail=f"limit {req.limit_price} is more than 25% above ask "
                   f"{quote['ask']} - resubmit with allow_above_ask if intentional",
        )

    try:
        order = await deps.broker.submit_order(occ, req.qty, req.limit_price)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=f"order rejected: {exc}")

    # Auto-journal the entry with the regime + score snapshot at entry time.
    regime = None
    try:
        regime = await deps.regime.compute()
    except Exception:
        pass
    cached = deps.scanner.find_cached(occ)
    journal_id = db.execute(
        """
        INSERT INTO journal (
          created_at, order_id, occ_symbol, underlying, side, qty, limit_price,
          status, regime_label, regime_score, score_total, score_breakdown,
          paper, broker
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            dt.datetime.now().isoformat(timespec="seconds"),
            order.get("id"),
            occ,
            (cached or {}).get("underlying") or _occ_underlying(occ),
            "buy",
            req.qty,
            req.limit_price,
            order.get("status"),
            regime["label"] if regime else None,
            regime["score"] if regime else None,
            (cached or {}).get("score"),
            json.dumps(cached["components"]) if cached and cached.get("components") else None,
            1 if deps.broker.paper else 0,
            deps.broker_name,
        ),
    )
    return {"order": _trim_order(order), "journal_id": journal_id,
            "broker": deps.broker_name}


@router.get("/orders/{order_id}")
async def order_status(order_id: str) -> Dict[str, Any]:
    try:
        order = await get_deps().broker.get_order(order_id)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"order": _trim_order(order)}


# ---------------------------------------------------------------- journal

@router.get("/journal")
async def journal_list(limit: int = Query(100, ge=1, le=1000)) -> Dict[str, Any]:
    rows = db.query("SELECT * FROM journal ORDER BY id DESC LIMIT ?", (limit,))
    for row in rows:
        row["score_breakdown"] = (
            json.loads(row["score_breakdown"]) if row["score_breakdown"] else None
        )
    return {"items": rows}


@router.get("/journal/stats")
async def journal_stats() -> Dict[str, Any]:
    closed = db.query("SELECT * FROM journal WHERE closed_at IS NOT NULL")
    open_count = db.query(
        "SELECT COUNT(*) AS n FROM journal WHERE closed_at IS NULL"
    )[0]["n"]
    wins = [r for r in closed if (r["realized_pnl"] or 0) > 0]
    losses = [r for r in closed if (r["realized_pnl"] or 0) < 0]
    gross_profit = sum(r["realized_pnl"] or 0 for r in wins)
    gross_loss = abs(sum(r["realized_pnl"] or 0 for r in losses))
    holds = []
    for r in closed:
        try:
            delta = dt.datetime.fromisoformat(r["closed_at"]) - dt.datetime.fromisoformat(
                r["created_at"]
            )
            holds.append(delta.total_seconds() / 86400.0)
        except (TypeError, ValueError):
            pass
    return {
        "open": open_count,
        "closed": len(closed),
        "win_rate": round(len(wins) / len(closed), 3) if closed else None,
        "profit_factor": (
            round(gross_profit / gross_loss, 2) if gross_loss > 0
            else (None if not wins else "inf")
        ),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "net_pnl": round(gross_profit - gross_loss, 2),
        "avg_hold_days": round(sum(holds) / len(holds), 1) if holds else None,
    }


@router.patch("/journal/{journal_id}")
async def journal_notes(journal_id: int, req: NotesRequest) -> Dict[str, Any]:
    db.execute(
        "UPDATE journal SET notes = ? WHERE id = ?", (req.notes[:2000], journal_id)
    )
    return {"ok": True}


@router.post("/journal/{journal_id}/close")
async def journal_close(journal_id: int, req: CloseRequest) -> Dict[str, Any]:
    rows = db.query("SELECT * FROM journal WHERE id = ?", (journal_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="journal entry not found")
    entry = rows[0]
    if entry["closed_at"]:
        raise HTTPException(status_code=400, detail="entry already closed")
    basis = entry["filled_avg_price"] or entry["limit_price"] or 0.0
    pnl = round((req.exit_price - basis) * 100.0 * entry["qty"], 2)
    db.execute(
        "UPDATE journal SET closed_at = ?, exit_price = ?, realized_pnl = ? WHERE id = ?",
        (
            dt.datetime.now().isoformat(timespec="seconds"),
            req.exit_price, pnl, journal_id,
        ),
    )
    return {"ok": True, "realized_pnl": pnl, "basis_used": basis}


@router.post("/journal/{journal_id}/sync")
async def journal_sync(journal_id: int) -> Dict[str, Any]:
    """Refresh order status and fill price from Alpaca."""
    rows = db.query("SELECT * FROM journal WHERE id = ?", (journal_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="journal entry not found")
    entry = rows[0]
    if not entry["order_id"]:
        raise HTTPException(status_code=400, detail="entry has no order id")
    deps = get_deps()
    broker = deps._clients.get(entry.get("broker"), deps.alpaca)
    try:
        order = await broker.get_order(entry["order_id"])
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    filled = order.get("filled_avg_price")
    db.execute(
        "UPDATE journal SET status = ?, filled_avg_price = ? WHERE id = ?",
        (order.get("status"), float(filled) if filled else None, journal_id),
    )
    return {"ok": True, "order": _trim_order(order)}


# ----------------------------------------------------------------- alerts

@router.get("/alerts")
async def alerts(unseen_only: bool = Query(False),
                 limit: int = Query(50, ge=1, le=500)) -> Dict[str, Any]:
    sql = "SELECT * FROM alerts"
    if unseen_only:
        sql += " WHERE seen = 0"
    sql += " ORDER BY id DESC LIMIT ?"
    rows = db.query(sql, (limit,))
    for row in rows:
        row["payload"] = json.loads(row["payload"]) if row["payload"] else None
    return {"items": rows}


@router.post("/alerts/mark-seen")
async def alerts_mark_seen() -> Dict[str, Any]:
    db.execute("UPDATE alerts SET seen = 1 WHERE seen = 0")
    return {"ok": True}


# ----------------------------------------------- score-validation tracker

class SnapshotRequest(BaseModel):
    top_n: int = Field(25, ge=1, le=200)
    sector: Optional[str] = None
    theme: Optional[str] = None
    dte: Optional[str] = None


@router.post("/tracking/snapshot")
async def tracking_snapshot(req: SnapshotRequest) -> Dict[str, Any]:
    try:
        return await get_deps().tracker.snapshot(
            top_n=req.top_n, sector=req.sector, theme=req.theme, dte=req.dte
        )
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/tracking/update")
async def tracking_update() -> Dict[str, Any]:
    return await get_deps().tracker.update_outcomes()


@router.get("/tracking/report")
async def tracking_report() -> Dict[str, Any]:
    return get_deps().tracker.report()


@router.get("/tracking")
async def tracking_recent(limit: int = Query(100, ge=1, le=1000)) -> Dict[str, Any]:
    return {"items": get_deps().tracker.recent(limit)}
