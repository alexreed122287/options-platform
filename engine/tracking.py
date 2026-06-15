"""Score-validation tracker (forward backtest).

Historical per-contract option data is not available on these feeds, so this
measures the score's edge GOING FORWARD: snapshot the current top-scoring
contracts with their entry mid, then re-quote them over the following days and
record the realized option return. The report buckets outcomes by entry score
so you can see whether higher scores actually produced better returns.

It accumulates - a single day proves nothing; a few weeks of snapshots start
to. Nothing here trades; it only reads quotes.
"""
import datetime as dt
import logging
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from api import db
from data.base import ProviderError

log = logging.getLogger("engine.tracking")

ET = ZoneInfo("America/New_York")

# entry-score buckets for the report
BUCKETS = [(0, 50), (50, 60), (60, 70), (70, 80), (80, 90), (90, 1000)]


class ScoreTracker:
    def __init__(self, scanner, broker, cache):
        self.scanner = scanner
        self.broker = broker
        self.cache = cache

    async def snapshot(self, top_n: int = 25, sector: Optional[str] = None,
                       theme: Optional[str] = None, dte: Optional[str] = None) -> Dict[str, Any]:
        """Record today's top-N scored contracts as forward-test entries
        (deduped per contract per ET day)."""
        scan = await self.scanner.scan(sector=sector, theme=theme, dte=dte)
        results = sorted(scan.get("results", []), key=lambda r: r["score"], reverse=True)[:top_n]
        now = dt.datetime.now(ET)
        today = now.date().isoformat()
        inserted = 0
        for r in results:
            n = db.execute_rc(
                """
                INSERT OR IGNORE INTO score_tracking
                  (snapshot_date, snapshot_at, occ_symbol, underlying, expiration,
                   dte, delta, score, entry_mid, entry_spot, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
                """,
                (today, now.isoformat(timespec="seconds"), r["occ_symbol"],
                 r.get("underlying"), r.get("expiration"), r.get("dte"),
                 r.get("delta"), r.get("score"), r.get("mid"), r.get("spot")),
            )
            inserted += n
        log.info("score snapshot: %d new entries (top %d)", inserted, top_n)
        return {"snapshot_date": today, "candidates": len(results), "new_entries": inserted}

    async def update_outcomes(self, limit: int = 250) -> Dict[str, Any]:
        """Re-quote open tracked contracts and record realized returns. Marks
        contracts past expiration as expired (final mark)."""
        rows = db.query(
            "SELECT * FROM score_tracking WHERE status = 'open' "
            "ORDER BY snapshot_date LIMIT ?", (limit,),
        )
        now = dt.datetime.now(ET)
        today = now.date()
        updated, expired, errors = 0, 0, 0
        spot_cache: Dict[str, Optional[float]] = {}
        for row in rows:
            occ = row["occ_symbol"]
            try:
                quote = await self.broker.option_latest_quote(occ)
                current_mid = quote.get("mid")
            except ProviderError as exc:
                errors += 1
                log.debug("tracking re-quote failed for %s: %s", occ, exc)
                continue
            underlying = row.get("underlying")
            if underlying and underlying not in spot_cache:
                try:
                    snap = await self.broker.stock_snapshot(underlying)
                    spot_cache[underlying] = snap.data.get("price")
                except ProviderError:
                    spot_cache[underlying] = None
            current_spot = spot_cache.get(underlying)

            opt_ret = (
                round((current_mid - row["entry_mid"]) / row["entry_mid"] * 100.0, 2)
                if current_mid and row.get("entry_mid") else None
            )
            und_ret = (
                round((current_spot - row["entry_spot"]) / row["entry_spot"] * 100.0, 2)
                if current_spot and row.get("entry_spot") else None
            )
            try:
                snap_date = dt.date.fromisoformat(row["snapshot_date"])
                days_held = (today - snap_date).days
            except (ValueError, TypeError):
                days_held = None
            status = "open"
            try:
                if row.get("expiration") and dt.date.fromisoformat(row["expiration"]) <= today:
                    status = "expired"
                    expired += 1
            except (ValueError, TypeError):
                pass
            db.execute(
                """
                UPDATE score_tracking SET updated_at = ?, current_mid = ?,
                  current_spot = ?, option_return_pct = ?, underlying_return_pct = ?,
                  days_held = ?, status = ? WHERE id = ?
                """,
                (now.isoformat(timespec="seconds"), current_mid, current_spot,
                 opt_ret, und_ret, days_held, status, row["id"]),
            )
            updated += 1
        log.info("score outcomes updated: %d (expired %d, errors %d)", updated, expired, errors)
        return {"updated": updated, "expired": expired, "errors": errors,
                "remaining_open": len(rows) - updated}

    def report(self) -> Dict[str, Any]:
        """Bucket tracked entries with a recorded outcome by entry score, and
        show count, avg option/underlying return, and win rate per bucket."""
        rows = db.query(
            "SELECT score, option_return_pct, underlying_return_pct, days_held "
            "FROM score_tracking WHERE option_return_pct IS NOT NULL"
        )
        buckets = []
        for lo, hi in BUCKETS:
            group = [r for r in rows if lo <= r["score"] < hi]
            if not group:
                buckets.append({"range": f"{lo}-{hi if hi < 1000 else '100+'}",
                                "count": 0, "avg_option_return": None,
                                "avg_underlying_return": None, "win_rate": None,
                                "avg_days_held": None})
                continue
            opt = [r["option_return_pct"] for r in group if r["option_return_pct"] is not None]
            und = [r["underlying_return_pct"] for r in group if r["underlying_return_pct"] is not None]
            held = [r["days_held"] for r in group if r["days_held"] is not None]
            wins = [x for x in opt if x > 0]
            buckets.append({
                "range": f"{lo}-{hi if hi < 1000 else '100+'}",
                "count": len(group),
                "avg_option_return": round(sum(opt) / len(opt), 2) if opt else None,
                "avg_underlying_return": round(sum(und) / len(und), 2) if und else None,
                "win_rate": round(len(wins) / len(opt), 3) if opt else None,
                "avg_days_held": round(sum(held) / len(held), 1) if held else None,
            })
        totals = db.query(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN option_return_pct IS NOT NULL THEN 1 ELSE 0 END) AS with_outcome, "
            "SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open_n "
            "FROM score_tracking"
        )[0]
        return {
            "buckets": buckets,
            "total_tracked": totals["total"] or 0,
            "with_outcome": totals["with_outcome"] or 0,
            "open": totals["open_n"] or 0,
        }

    def recent(self, limit: int = 100) -> List[Dict[str, Any]]:
        return db.query(
            "SELECT snapshot_date, occ_symbol, underlying, score, entry_mid, "
            "current_mid, option_return_pct, underlying_return_pct, days_held, status "
            "FROM score_tracking ORDER BY snapshot_date DESC, score DESC LIMIT ?",
            (max(1, min(limit, 1000)),),
        )
