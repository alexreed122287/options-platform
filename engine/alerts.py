"""Background alert loop.

Periodically rescans the universe and records an alert for any contract whose
score crosses the configured threshold (one alert per contract per ET day).
Alerts ONLY notify - this loop never submits orders and has no code path to
the order endpoint. The loop never dies on errors; failures are surfaced in
/api/health and the dashboard banner instead of being swallowed.
"""
import asyncio
import datetime as dt
import json
import logging
import time
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from api import db
from data.base import ProviderError

log = logging.getLogger("engine.alerts")

ET = ZoneInfo("America/New_York")


class AlertLoop:
    def __init__(self, scanner, market_data, config):
        self.scanner = scanner
        self.market_data = market_data  # alpaca or public, per DATA_SOURCE
        self.config = config
        self.tracker = None             # set by deps; daily score snapshots
        self._last_snapshot_date = None
        self.runs = 0
        self.last_run: Optional[str] = None
        self.last_skip: Optional[str] = None
        self.last_error: Optional[str] = None
        self.last_new_alerts = 0
        self._task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------- status

    def status(self) -> Dict[str, Any]:
        cfg = self.config.get("settings")["scan"]
        return {
            "enabled": bool(cfg.get("enabled", True)),
            "interval_minutes": cfg.get("interval_minutes"),
            "threshold": cfg.get("alert_score_threshold"),
            "market_hours_only": bool(cfg.get("market_hours_only", True)),
            "running": self._task is not None and not self._task.done(),
            "runs": self.runs,
            "last_run": self.last_run,
            "last_skip": self.last_skip,
            "last_error": self.last_error,
            "last_new_alerts": self.last_new_alerts,
        }

    # ------------------------------------------------------ alert writing

    def process_results(self, results: List[Dict[str, Any]], threshold: float) -> int:
        """Insert an alert for each contract scoring at/above threshold,
        deduped per contract per ET day. Returns the number of NEW alerts.
        Pure DB logic so it is testable without live providers."""
        now_et = dt.datetime.now(ET)
        today = now_et.date().isoformat()
        created = now_et.isoformat(timespec="seconds")
        new = 0
        for row in results:
            score = row.get("score")
            if score is None or score < threshold or not row.get("occ_symbol"):
                continue
            payload = {key: row.get(key) for key in (
                "underlying", "strike", "expiration", "dte", "mid", "delta",
                "score", "open_interest", "volume", "iv", "iv_rank",
            )}
            inserted = db.execute_rc(
                "INSERT OR IGNORE INTO alerts "
                "(created_at, date, occ_symbol, underlying, score, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (created, today, row["occ_symbol"], row.get("underlying"),
                 score, json.dumps(payload)),
            )
            if inserted:
                new += 1
                log.info("ALERT: %s scored %.1f (threshold %.1f)",
                         row["occ_symbol"], score, threshold)
        return new

    # --------------------------------------------------------------- loop

    async def _market_open(self) -> bool:
        """Market-hours check: broker clock when the data source has one
        (Alpaca), otherwise a deterministic ET weekday 9:30-16:00 window
        (Public exposes no clock endpoint)."""
        if hasattr(self.market_data, "clock"):
            try:
                clock = await self.market_data.clock()
                return bool(clock.data["is_open"])
            except ProviderError as exc:
                log.warning("clock unavailable, using ET window fallback: %s", exc)
        now = dt.datetime.now(ET)
        return (now.weekday() < 5
                and dt.time(9, 30) <= now.time() < dt.time(16, 0))

    async def _tick(self) -> None:
        cfg = self.config.get("settings")["scan"]
        self.last_skip = None
        if not cfg.get("enabled", True):
            self.last_skip = "scan disabled in config/settings.json"
            return
        if not self.market_data.configured:
            self.last_skip = f"{self.market_data.name} keys not configured - scan skipped"
            return
        if cfg.get("market_hours_only", True) and not await self._market_open():
            self.last_skip = "market closed"
            return

        result = await self.scanner.scan(refresh=True)
        threshold = float(cfg.get("alert_score_threshold", 75))
        self.last_new_alerts = self.process_results(result.get("results", []), threshold)

        # once per ET day, snapshot the top picks for the forward score-validation
        today = dt.datetime.now(ET).date().isoformat()
        if self.tracker and self._last_snapshot_date != today and result.get("results"):
            try:
                await self.tracker.snapshot(top_n=int(cfg.get("snapshot_top_n", 25)))
                self._last_snapshot_date = today
            except Exception:
                log.exception("daily score snapshot failed")

        degraded = result.get("degraded") or []
        self.last_error = (
            "; ".join(f"{d['symbol']}: {d['error'][:80]}" for d in degraded[:3])
            if degraded else None
        )

    async def run_forever(self) -> None:
        log.info("alert loop started")
        while True:
            started = time.time()
            try:
                await self._tick()
                self.runs += 1
                self.last_run = dt.datetime.now(ET).isoformat(timespec="seconds")
            except asyncio.CancelledError:
                log.info("alert loop stopped")
                raise
            except Exception as exc:  # never let the loop die silently
                self.last_error = str(exc)[:300]
                log.exception("alert loop tick failed")
            interval_s = max(
                60.0,
                float(self.config.get("settings")["scan"].get("interval_minutes", 15)) * 60.0,
            )
            await asyncio.sleep(max(5.0, interval_s - (time.time() - started)))

    def start(self) -> asyncio.Task:
        self._task = asyncio.create_task(self.run_forever())
        return self._task

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
