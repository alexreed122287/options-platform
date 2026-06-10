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
    def __init__(self, scanner, alpaca, config):
        self.scanner = scanner
        self.alpaca = alpaca
        self.config = config
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

    async def _tick(self) -> None:
        cfg = self.config.get("settings")["scan"]
        self.last_skip = None
        if not cfg.get("enabled", True):
            self.last_skip = "scan disabled in config/settings.json"
            return
        if not self.alpaca.configured:
            self.last_skip = "alpaca keys not configured - scan skipped"
            return
        if cfg.get("market_hours_only", True):
            try:
                clock = await self.alpaca.clock()
                if not clock.data["is_open"]:
                    self.last_skip = "market closed"
                    return
            except ProviderError as exc:
                self.last_error = f"clock unavailable: {exc}"
                return

        result = await self.scanner.scan(refresh=True)
        threshold = float(cfg.get("alert_score_threshold", 75))
        self.last_new_alerts = self.process_results(result.get("results", []), threshold)
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
