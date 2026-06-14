"""Market condition engine.

Composite regime score (0-100) from four components, each scored 0..1:
  spy_trend        10/20/50 EMA structure of SPY
  breadth          universe %-positive blended with sectors %-positive
  vix              VIX level + direction over a lookback window
  sector_rotation  offensive minus defensive sector day performance

Weights and thresholds live in config/regime.json. A component whose
provider is down scores neutral (0.5) and is flagged degraded - the engine
never fails silently and never crashes the API.
"""
import datetime as dt
import json
import logging
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from api import db
from data.base import ProviderError
from engine.indicators import clamp01, trend_structure

log = logging.getLogger("engine.regime")

ET = ZoneInfo("America/New_York")

NEUTRAL = 0.5


def _short(exc: Exception, limit: int = 140) -> str:
    return str(exc)[:limit]


def _component(score: float, detail: Dict[str, Any], degraded: bool = False,
               stale: bool = False, note: Optional[str] = None) -> Dict[str, Any]:
    return {
        "score": round(score, 3),
        "detail": detail,
        "degraded": degraded,
        "stale": stale,
        "note": note,
    }


class RegimeEngine:
    def __init__(self, fmp, alpaca, config, cache):
        self.fmp = fmp
        self.alpaca = alpaca
        self.config = config
        self.cache = cache

    # ------------------------------------------------------------ components

    async def _spy_trend(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        days = int(cfg.get("spy", {}).get("bars_days", 120))
        closes: Optional[List[float]] = None
        stale = False
        source = None
        note = None
        try:
            fetched = await self.alpaca.bars("SPY", days)
            closes = [row["close"] for row in fetched.data]
            stale, source = fetched.stale, "alpaca"
        except ProviderError as alpaca_exc:
            try:
                fetched = await self.fmp.history("SPY", days)
                closes = [row["close"] for row in fetched.data]
                stale, source = fetched.stale, "fmp"
                note = f"alpaca bars unavailable, used FMP history ({_short(alpaca_exc)})"
            except ProviderError as fmp_exc:
                return _component(
                    NEUTRAL, {"reason": "no price source available"}, degraded=True,
                    note=f"alpaca: {_short(alpaca_exc)} | fmp: {_short(fmp_exc)}",
                )
        result = trend_structure(closes)
        detail = dict(result["detail"])
        detail["source"] = source
        return _component(result["score"], detail, stale=stale, note=note)

    async def _breadth(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        bcfg = cfg.get("breadth", {})
        # a small representative sample - NOT the full scan universe (which can
        # be thousands of names and would blow the batch-quote URL limit)
        universe_cfg = self.config.get("universe")
        universe = universe_cfg.get("breadth_sample") or universe_cfg["tickers"][:20]
        parts: List[Dict[str, Any]] = []
        stale = False
        notes: List[str] = []

        try:
            fetched = await self.fmp.batch_quotes(universe)
            quotes = fetched.data
            stale = stale or fetched.stale
            positive = sum(
                1 for q in quotes.values()
                if q.get("change_pct") is not None and q["change_pct"] > 0
            )
            total = sum(1 for q in quotes.values() if q.get("change_pct") is not None)
            if total:
                parts.append({
                    "name": "universe",
                    "weight": float(bcfg.get("universe_weight", 0.5)),
                    "score": positive / total,
                    "positive": positive,
                    "total": total,
                })
        except ProviderError as exc:
            notes.append(f"universe breadth unavailable: {_short(exc)}")

        try:
            fetched = await self.fmp.sector_performance()
            sectors = fetched.data
            stale = stale or fetched.stale
            positive = sum(1 for s in sectors if s["change_pct"] > 0)
            if sectors:
                parts.append({
                    "name": "sectors",
                    "weight": float(bcfg.get("sectors_weight", 0.5)),
                    "score": positive / len(sectors),
                    "positive": positive,
                    "total": len(sectors),
                })
        except ProviderError as exc:
            notes.append(f"sector breadth unavailable: {_short(exc)}")

        if not parts:
            return _component(NEUTRAL, {"reason": "no breadth inputs"}, degraded=True,
                              note=" | ".join(notes) or None)
        weight_sum = sum(p["weight"] for p in parts)
        score = sum(p["score"] * p["weight"] for p in parts) / weight_sum
        detail = {p["name"]: f"{p['positive']}/{p['total']} positive" for p in parts}
        return _component(score, detail, degraded=bool(notes), stale=stale,
                          note=" | ".join(notes) or None)

    async def _vix(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        vcfg = cfg.get("vix", {})
        low = float(vcfg.get("level_low", 15))
        high = float(vcfg.get("level_high", 25))
        lookback = int(vcfg.get("direction_lookback_sessions", 5))
        scale = float(vcfg.get("direction_scale_pct", 10))
        level_w = float(vcfg.get("level_weight", 0.7))
        dir_w = float(vcfg.get("direction_weight", 0.3))

        try:
            fetched = await self.fmp.vix()
            level = fetched.data["price"]
            stale = fetched.stale
        except ProviderError as exc:
            return _component(NEUTRAL, {"reason": "vix quote unavailable"}, degraded=True,
                              note=_short(exc))
        if level is None:
            return _component(NEUTRAL, {"reason": "vix quote empty"}, degraded=True)

        level_score = clamp01((high - level) / (high - low)) if high > low else NEUTRAL

        direction_score = NEUTRAL
        change_pct = None
        note = None
        try:
            hist = await self.fmp.history("^VIX", lookback + 10)
            closes = [row["close"] for row in hist.data]
            stale = stale or hist.stale
            if len(closes) > lookback:
                change_pct = (closes[-1] / closes[-1 - lookback] - 1.0) * 100.0
                direction_score = clamp01((scale - change_pct) / (2.0 * scale))
        except ProviderError as exc:
            note = f"vix history unavailable, direction neutral: {_short(exc)}"

        score = level_w * level_score + dir_w * direction_score
        return _component(score, {
            "level": round(level, 2),
            "level_score": round(level_score, 3),
            "change_pct_lookback": round(change_pct, 2) if change_pct is not None else None,
            "direction_score": round(direction_score, 3),
        }, degraded=note is not None, stale=stale, note=note)

    async def _sector_rotation(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        rcfg = cfg.get("sector_rotation", {})
        scale = float(rcfg.get("scale_pct", 1.0))
        offensive = set(rcfg.get("offensive", []))
        defensive = set(rcfg.get("defensive", []))
        try:
            fetched = await self.fmp.sector_performance()
        except ProviderError as exc:
            return _component(NEUTRAL, {"reason": "sector performance unavailable"},
                              degraded=True, note=_short(exc))
        sectors = {s["sector"]: s["change_pct"] for s in fetched.data}
        off = [v for k, v in sectors.items() if k in offensive]
        deff = [v for k, v in sectors.items() if k in defensive]
        if not off or not deff:
            return _component(NEUTRAL, {"reason": "sector names did not match config"},
                              degraded=True,
                              note=f"sectors seen: {sorted(sectors)[:6]}")
        off_avg = sum(off) / len(off)
        def_avg = sum(deff) / len(deff)
        diff = off_avg - def_avg
        score = clamp01((diff + scale) / (2.0 * scale))
        return _component(score, {
            "offensive_avg_pct": round(off_avg, 3),
            "defensive_avg_pct": round(def_avg, 3),
            "diff_pct": round(diff, 3),
        }, stale=fetched.stale)

    # --------------------------------------------------------------- compute

    async def _compute_now(self) -> Dict[str, Any]:
        cfg = self.config.get("regime")
        weights: Dict[str, float] = cfg["weights"]

        components = {
            "spy_trend": await self._spy_trend(cfg),
            "breadth": await self._breadth(cfg),
            "vix": await self._vix(cfg),
            "sector_rotation": await self._sector_rotation(cfg),
        }
        weight_sum = sum(float(weights[name]) for name in components)
        score = 100.0 * sum(
            components[name]["score"] * float(weights[name]) for name in components
        ) / weight_sum
        score = round(score, 1)

        labels = cfg.get("labels", {})
        if score >= float(labels.get("risk_on_min", 65)):
            label = "risk-on"
        elif score <= float(labels.get("risk_off_max", 40)):
            label = "risk-off"
        else:
            label = "neutral"

        for name in components:
            components[name]["weight"] = float(weights[name])

        now_et = dt.datetime.now(ET)
        result = {
            "as_of": now_et.isoformat(timespec="seconds"),
            "date": now_et.date().isoformat(),
            "score": score,
            "label": label,
            "components": components,
            "degraded": any(c["degraded"] for c in components.values()),
            "stale": any(c["stale"] for c in components.values()),
        }
        self._persist(result)
        return result

    def _persist(self, result: Dict[str, Any]) -> None:
        try:
            db.execute(
                """
                INSERT INTO regime_snapshots (date, score, label, components, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                  score = excluded.score,
                  label = excluded.label,
                  components = excluded.components,
                  created_at = excluded.created_at
                """,
                (
                    result["date"], result["score"], result["label"],
                    json.dumps(result["components"]), result["as_of"],
                ),
            )
        except Exception:
            log.exception("failed to persist regime snapshot")

    async def compute(self, refresh: bool = False) -> Dict[str, Any]:
        ttl = self.config.get("settings")["cache_ttls_seconds"]["regime"]
        if refresh:
            result = await self._compute_now()
            self.cache.set("regime:result", result, ttl)
            return result
        fetched = await self.cache.get_or_fetch("regime:result", ttl, self._compute_now)
        result = dict(fetched.data)
        if fetched.stale:
            result["stale"] = True
        return result

    def history(self, limit: int = 30) -> List[Dict[str, Any]]:
        rows = db.query(
            "SELECT date, score, label, created_at FROM regime_snapshots "
            "ORDER BY date DESC LIMIT ?",
            (max(1, min(limit, 365)),),
        )
        return rows
