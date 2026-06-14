"""Contract scoring engine.

Pure scoring functions (deterministic, unit-tested) plus the Scanner that
applies them across the configured universe. Every component of a score is
returned in the breakdown - raw value, 0..1 score, normalized weight, and
contribution - so the UI can show exactly why a contract ranked where it did.
"""
import asyncio
import datetime as dt
import logging
import math
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from api import db
from data.base import ProviderError
from engine.indicators import clamp01, trend_structure

log = logging.getLogger("engine.scoring")

ET = ZoneInfo("America/New_York")


# ------------------------------------------------------ pure scoring math

def linear_score(value: Optional[float], best: float, worst: float) -> float:
    """1.0 at `best`, 0.0 at `worst`, linear in between; works in either
    direction (best below or above worst)."""
    if value is None:
        return 0.0
    if best == worst:
        return 1.0 if value == best else 0.0
    return clamp01((value - worst) / (best - worst))


def band_score(value: Optional[float], lo: float, hi: float, falloff: float) -> float:
    """1.0 inside [lo, hi]; linear falloff to 0.0 over `falloff` outside."""
    if value is None:
        return 0.0
    if lo <= value <= hi:
        return 1.0
    if falloff <= 0:
        return 0.0
    if value < lo:
        return clamp01(1.0 - (lo - value) / falloff)
    return clamp01(1.0 - (value - hi) / falloff)


def score_delta_fit(delta: Optional[float], cfg: Dict[str, Any]) -> float:
    band = cfg["delta_band"]
    value = abs(delta) if delta is not None else None
    return band_score(value, band["min"], band["max"], band["falloff"])


def extrinsic_pct(mid: Optional[float], strike: float, spot: Optional[float],
                  side: str = "call") -> Optional[float]:
    """Extrinsic premium as a fraction of total premium (0..1)."""
    if not mid or mid <= 0 or not spot:
        return None
    intrinsic = max(0.0, (spot - strike) if side == "call" else (strike - spot))
    extrinsic = max(0.0, mid - intrinsic)
    return min(1.0, extrinsic / mid)


def score_extrinsic(ext_pct: Optional[float], cfg: Dict[str, Any]) -> float:
    e = cfg["extrinsic"]
    if ext_pct is None:
        return 0.0
    if ext_pct <= e["best_max_pct"]:
        return 1.0
    return linear_score(ext_pct, e["best_max_pct"], e["worst_pct"])


def spread_pct(contract: Dict[str, Any]) -> Optional[float]:
    bid, ask, mid = contract.get("bid"), contract.get("ask"), contract.get("mid")
    if not mid or mid <= 0 or bid is None or ask is None:
        return None
    return max(0.0, (ask - bid) / mid)


def score_spread(spr_pct: Optional[float], cfg: Dict[str, Any]) -> float:
    s = cfg["spread"]
    if spr_pct is None:
        return 0.0
    if spr_pct <= s["best_max_pct"]:
        return 1.0
    return linear_score(spr_pct, s["best_max_pct"], s["worst_pct"])


def score_open_interest(oi: Optional[float], cfg: Dict[str, Any]) -> float:
    o = cfg["open_interest"]
    if oi is None:
        return 0.0
    return clamp01((oi - o["floor"]) / max(1.0, o["full_credit"] - o["floor"]))


def score_volume(volume: Optional[float], cfg: Dict[str, Any]) -> float:
    v = cfg["volume"]
    if volume is None:
        return 0.0
    return clamp01((volume - v["floor"]) / max(1.0, v["full_credit"] - v["floor"]))


def score_iv_rank(rank: Optional[float], cfg: Dict[str, Any]) -> float:
    """rank = percentile (0..1) of today's ATM IV vs trailing history.
    None (insufficient history) scores neutral 0.5."""
    if rank is None:
        return 0.5
    return 1.0 - rank if cfg["iv_rank"].get("prefer_low", True) else rank


def score_dte_fit(dte: Optional[float], cfg: Dict[str, Any]) -> float:
    band = cfg["dte_band"]
    return band_score(dte, band["min"], band["max"], band["falloff_days"])


def score_trend_alignment(trend01: Optional[float], regime01: Optional[float],
                          cfg: Dict[str, Any]) -> float:
    t = cfg["trend_alignment"]
    tw, rw = float(t["trend_weight"]), float(t["regime_weight"])
    if tw + rw <= 0:
        return 0.5
    trend01 = 0.5 if trend01 is None else trend01
    regime01 = 0.5 if regime01 is None else regime01
    return clamp01((trend01 * tw + regime01 * rw) / (tw + rw))


def prefilter_score(quote: Dict[str, Any], cfg: Dict[str, Any]) -> Optional[float]:
    """Cheap 0..1 candidate score from a plain quote, for narrowing a large
    universe before the expensive chain scan. Trend (price vs 50/200-day MA),
    day momentum, and liquidity, weighted per config. Returns None when the
    name is below the liquidity floor or has no usable price."""
    price = quote.get("price")
    volume = quote.get("volume") or 0
    if not price or price <= 0:
        return None
    if volume < cfg.get("min_volume", 0):
        return None
    weights = cfg.get("weights", {})

    ma50, ma200 = quote.get("ma50"), quote.get("ma200")
    trend_pts, trend_n = 0.0, 0
    if ma50:
        trend_pts += 1.0 if price > ma50 else 0.0
        trend_n += 1
    if ma200:
        trend_pts += 1.0 if price > ma200 else 0.0
        trend_n += 1
    if trend_n:
        trend = trend_pts / trend_n
    elif quote.get("week_52_high"):
        # no moving averages (broker quotes) -> proximity to 52-week high as a
        # trend proxy: near the highs reads as a strong uptrend
        trend = clamp01(price / float(quote["week_52_high"]))
    else:
        trend = 0.5

    change = quote.get("change_pct")
    momentum = clamp01((change + 5.0) / 10.0) if change is not None else 0.5  # -5%..+5%

    liquidity = clamp01(math.log10(volume + 1.0) / 8.0)  # ~1 at 100M shares

    return (
        float(weights.get("trend", 0.5)) * trend
        + float(weights.get("momentum", 0.3)) * momentum
        + float(weights.get("liquidity", 0.2)) * liquidity
    )


def prefilter_rank(quotes: Dict[str, Dict[str, Any]], cfg: Dict[str, Any]) -> List[Tuple[str, float]]:
    """Rank a {symbol: quote} map by prefilter_score, descending. Names below
    the liquidity floor or without a price are dropped."""
    ranked = []
    for symbol, quote in quotes.items():
        score = prefilter_score(quote, cfg)
        if score is not None:
            ranked.append((symbol, round(score, 4)))
    ranked.sort(key=lambda r: r[1], reverse=True)
    return ranked


def passes_filters(contract: Dict[str, Any], cfg: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    f = cfg["filters"]
    mid = contract.get("mid")
    if mid is None or mid < f["min_mid"]:
        return False, "no quote or mid below min_mid"
    if (contract.get("bid") or 0) <= 0 or (contract.get("ask") or 0) <= 0:
        return False, "missing bid/ask"
    if f.get("require_greeks", True) and contract.get("delta") is None:
        return False, "missing greeks"
    if (contract.get("open_interest") or 0) < f["min_open_interest"]:
        return False, "open interest below minimum"
    if (contract.get("volume") or 0) < f["min_volume"]:
        return False, "volume below minimum"
    spr = spread_pct(contract)
    if spr is None or spr > f["max_spread_pct"]:
        return False, "spread too wide"
    return True, None


def compute_contract_score(contract: Dict[str, Any], ctx: Dict[str, Any],
                           cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Score one contract against config weights.

    ctx: {spot, trend01, regime01, iv_rank} - iv_rank may be None.
    Returns {"total": 0-100, "components": [...]} with every component's raw
    value, 0..1 score, normalized weight, contribution, and optional note.
    """
    weights = cfg["weights"]
    wsum = sum(float(w) for w in weights.values())
    side = cfg.get("side", "call")

    ext = extrinsic_pct(contract.get("mid"), contract.get("strike"), ctx.get("spot"), side)
    spr = spread_pct(contract)
    iv_rank = ctx.get("iv_rank")
    trend01, regime01 = ctx.get("trend01"), ctx.get("regime01")
    alignment = score_trend_alignment(trend01, regime01, cfg)

    rows = [
        ("delta_fit", contract.get("delta"),
         score_delta_fit(contract.get("delta"), cfg), None),
        ("extrinsic", ext, score_extrinsic(ext, cfg),
         "extrinsic premium / mid"),
        ("spread", spr, score_spread(spr, cfg),
         "(ask - bid) / mid"),
        ("open_interest", contract.get("open_interest"),
         score_open_interest(contract.get("open_interest"), cfg), None),
        ("volume", contract.get("volume"),
         score_volume(contract.get("volume"), cfg), None),
        ("iv_rank", iv_rank, score_iv_rank(iv_rank, cfg),
         None if iv_rank is not None else "insufficient IV history - neutral 0.5 applied"),
        ("dte_fit", contract.get("dte"),
         score_dte_fit(contract.get("dte"), cfg), None),
        ("trend_alignment", alignment, alignment,
         f"underlying trend {trend01 if trend01 is not None else 'n/a'} "
         f"blended with regime {regime01 if regime01 is not None else 'n/a'}"),
    ]

    components = []
    total = 0.0
    for name, raw, score, note in rows:
        weight = float(weights[name]) / wsum
        contribution = score * weight
        total += contribution
        components.append({
            "name": name,
            "raw": round(raw, 4) if isinstance(raw, (int, float)) else raw,
            "score": round(score, 4),
            "weight": round(weight, 4),
            "contribution": round(contribution, 4),
            "note": note,
        })
    return {"total": round(total * 100.0, 1), "components": components}


# --------------------------------------------------------------- scanner

class Scanner:
    def __init__(self, fmp, market_data, regime_engine, config, cache):
        self.fmp = fmp
        self.market_data = market_data  # alpaca or public, per DATA_SOURCE
        self.regime = regime_engine
        self.config = config
        self.cache = cache

    def _iv_rank(self, symbol: str, chain_rows: List[Dict[str, Any]], spot: float,
                 cfg: Dict[str, Any]) -> Optional[float]:
        """Persist today's ATM IV for `symbol` and return the current
        percentile vs the trailing 90 days. None until enough history."""
        band = cfg["dte_band"]
        candidates = [
            c for c in chain_rows
            if c.get("iv") and c.get("dte") is not None
            and band["min"] <= c["dte"] <= band["max"]
        ]
        atm_iv = None
        if candidates:
            atm = min(candidates, key=lambda c: abs(c["strike"] - spot))
            atm_iv = float(atm["iv"])
        today_et = dt.datetime.now(ET).date().isoformat()
        if atm_iv:
            try:
                db.execute(
                    "INSERT INTO iv_history (symbol, date, atm_iv) VALUES (?, ?, ?) "
                    "ON CONFLICT(symbol, date) DO UPDATE SET atm_iv = excluded.atm_iv",
                    (symbol, today_et, atm_iv),
                )
            except Exception:
                log.exception("failed to persist ATM IV for %s", symbol)
        min_days = int(cfg["iv_rank"].get("min_history_days", 10))
        rows = db.query(
            "SELECT atm_iv FROM iv_history WHERE symbol = ? "
            "AND date >= date('now', '-90 day') ORDER BY date",
            (symbol,),
        )
        if atm_iv is None or len(rows) < min_days:
            return None
        values = [r["atm_iv"] for r in rows]
        below = sum(1 for v in values if v <= atm_iv)
        return round(below / len(values), 3)

    async def _scan_ticker(self, symbol: str, regime01: float, cfg: Dict[str, Any],
                           sem: asyncio.Semaphore) -> Dict[str, Any]:
        async with sem:
            try:
                snap = await self.market_data.stock_snapshot(symbol)
                spot = snap.data.get("price")
                stale = snap.stale
                if not spot:
                    return {"symbol": symbol, "rows": [], "scanned": 0, "dropped": {},
                            "stale": stale, "error": "no spot price"}

                # Trend: data-source bars first, FMP history as fallback
                # (Public has no bars endpoint at all).
                trend01 = None
                closes = None
                try:
                    bars = await self.market_data.bars(symbol, 120)
                    closes = [r["close"] for r in bars.data]
                    stale = stale or bars.stale
                except ProviderError:
                    try:
                        hist = await self.fmp.history(symbol, 120)
                        closes = [r["close"] for r in hist.data]
                        stale = stale or hist.stale
                    except ProviderError as exc:
                        log.warning("trend unavailable for %s: %s", symbol, exc)
                if closes:
                    trend01 = trend_structure(closes)["score"]

                today = dt.date.today()
                band = cfg["dte_band"]
                pad = int(band["falloff_days"])
                window = cfg["chain_window"]
                chain = await self.market_data.chain(
                    symbol,
                    cfg.get("side", "call"),
                    exp_gte=(today + dt.timedelta(days=max(0, int(band["min"]) - pad))).isoformat(),
                    exp_lte=(today + dt.timedelta(days=int(band["max"]) + pad)).isoformat(),
                    strike_gte=spot * (1.0 - float(window["strike_itm_pct"])),
                    strike_lte=spot * (1.0 + float(window["strike_otm_pct"])),
                )
                stale = stale or chain.stale

                iv_rank = self._iv_rank(symbol, chain.data, spot, cfg)
                ctx = {"spot": spot, "trend01": trend01, "regime01": regime01,
                       "iv_rank": iv_rank}

                rows = []
                dropped: Dict[str, int] = {}
                for contract in chain.data:
                    ok, reason = passes_filters(contract, cfg)
                    if not ok:
                        dropped[reason] = dropped.get(reason, 0) + 1
                        continue
                    scored = compute_contract_score(contract, ctx, cfg)
                    row = dict(contract)
                    row["spot"] = spot
                    row["spread_pct"] = round(spread_pct(contract) or 0.0, 4)
                    row["trend01"] = trend01
                    row["iv_rank"] = iv_rank
                    row["score"] = scored["total"]
                    row["components"] = scored["components"]
                    rows.append(row)
                return {"symbol": symbol, "rows": rows, "scanned": len(chain.data),
                        "dropped": dropped, "stale": stale, "error": None}
            except ProviderError as exc:
                return {"symbol": symbol, "rows": [], "scanned": 0, "dropped": {},
                        "stale": False, "error": str(exc)}

    def _filtered_universe(self, sector: Optional[str], theme: Optional[str]) -> List[str]:
        universe = self.config.get("universe")["tickers"]
        segs = self.config.get("segments")
        if sector:
            sof = segs.get("sector_of", {})
            universe = [t for t in universe if sof.get(t) == sector]
        if theme:
            members = set(segs.get("themes", {}).get(theme, []))
            universe = [t for t in universe if t in members]
        return universe

    @staticmethod
    def _seg_key(sector: Optional[str], theme: Optional[str]) -> str:
        return f"{sector or 'all'}|{theme or 'all'}"

    def _resolve_dte_band(self, cfg: Dict[str, Any], dte_preset: Optional[str]) -> Dict[str, Any]:
        """The active DTE target: a named preset overrides the default dte_band
        (both the chain fetch window and dte_fit scoring follow it)."""
        base = cfg["dte_band"]
        if not dte_preset or dte_preset == "default":
            return base
        for opt in cfg.get("dte_presets", {}).get("options", []):
            if opt.get("key") == dte_preset:
                return {"min": opt["min"], "max": opt["max"],
                        "falloff_days": opt.get("falloff_days", base.get("falloff_days", 15))}
        return base

    async def _prefilter(self, universe: List[str], seg_key: str) -> Tuple[List[str], Dict[str, Any]]:
        """Stage 1: narrow a large universe to the top candidates with cheap
        batch quotes, before the expensive chain scan. Cached per segment for
        prefilter.cache_seconds so it is not repeated every scan refresh.
        Small universes (<= max_chain_scan) or disabled prefilter pass through."""
        pcfg = self.config.get("universe").get("prefilter", {})
        cap = int(pcfg.get("max_chain_scan", 40))
        info = {"prefiltered": False, "ranked": len(universe), "scanned": len(universe)}
        if not pcfg.get("enabled", False) or len(universe) <= cap:
            return universe, info

        # Prefilter quotes come from the active DATA SOURCE (Tradier/Alpaca),
        # which batch large symbol lists cheaply - NOT from FMP, whose plans
        # cap quote volume. Needs a configured data source.
        if not getattr(self.market_data, "configured", False) or \
                not hasattr(self.market_data, "batch_quotes"):
            info.update({"prefiltered": False, "scanned": min(cap, len(universe)),
                         "note": f"prefilter needs a configured data source "
                                 f"({self.market_data.name} not ready); scanning first {cap}"})
            return universe[:cap], info

        async def _rank() -> List[str]:
            fetched = await self.market_data.batch_quotes(universe)
            quotes = fetched.data
            ranked = prefilter_rank(quotes, pcfg)
            log.info("prefilter: ranked %d of %d names via %s, taking top %d",
                     len(ranked), len(universe), self.market_data.name, cap)
            return [sym for sym, _ in ranked[:cap]]

        cache_s = float(pcfg.get("cache_seconds", 1800))
        try:
            fetched = await self.cache.get_or_fetch(f"scan:prefilter:{seg_key}", cache_s, _rank)
            top = fetched.data
        except ProviderError as exc:
            log.warning("prefilter unavailable (%s); scanning first %d", exc, cap)
            info.update({"prefiltered": False, "scanned": min(cap, len(universe)),
                         "note": f"prefilter quote fetch failed; scanning first {cap}"})
            return universe[:cap], info
        if not top:
            top = universe[:cap]
            info["prefilter_error"] = True
        info.update({"prefiltered": True, "ranked": len(universe), "scanned": len(top)})
        return top, info

    async def _scan_now(self, sector: Optional[str] = None, theme: Optional[str] = None,
                        dte_preset: Optional[str] = None) -> Dict[str, Any]:
        base_cfg = self.config.get("scoring")
        dte_band = self._resolve_dte_band(base_cfg, dte_preset)
        cfg = {**base_cfg, "dte_band": dte_band}   # DTE filter applied here
        universe = self._filtered_universe(sector, theme)
        settings = self.config.get("settings")
        sector_of = self.config.get("segments").get("sector_of", {})
        regime = await self.regime.compute()
        regime01 = regime["score"] / 100.0
        seg = {"sector": sector, "theme": theme, "dte": dte_preset or "default",
               "dte_band": dte_band}

        if not universe:
            return {
                "as_of": dt.datetime.now(ET).isoformat(timespec="seconds"),
                "regime": {"label": regime["label"], "score": regime["score"]},
                "universe_size": 0, "segment": seg,
                "prefilter": {"prefiltered": False}, "tickers_scanned": 0,
                "contracts_scanned": 0, "contracts_kept": 0, "tickers_with_picks": 0,
                "dropped": {}, "groups": [], "results": [], "degraded": [],
                "stale": bool(regime.get("stale")),
            }

        scan_list, prefilter_info = await self._prefilter(universe, self._seg_key(sector, theme))

        sem = asyncio.Semaphore(int(settings["scan"].get("concurrency", 3)))
        per_ticker = await asyncio.gather(
            *[self._scan_ticker(sym, regime01, cfg, sem) for sym in scan_list]
        )

        rows = [row for result in per_ticker for row in result["rows"]]
        rows.sort(key=lambda r: r["score"], reverse=True)
        dropped: Dict[str, int] = {}
        for result in per_ticker:
            for reason, count in result["dropped"].items():
                dropped[reason] = dropped.get(reason, 0) + count

        # Group by underlying so the UI shows the single best contract per
        # ticker by default, expandable to that ticker's full ranked ladder.
        # rows are already globally score-sorted, so each ticker's list is too.
        per_ticker_cap = int(cfg.get("max_contracts_per_ticker", 20))
        top_tickers = int(cfg.get("top_n", 15))
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["underlying"], []).append(row)
        groups = [
            {
                "underlying": ticker,
                "sector": sector_of.get(ticker),
                "best_score": contracts[0]["score"],
                "best": contracts[0],
                "count": len(contracts),
                "contracts": contracts[:per_ticker_cap],
            }
            for ticker, contracts in grouped.items()
        ]
        groups.sort(key=lambda g: g["best_score"], reverse=True)
        groups = groups[:top_tickers]
        # flat union of shown contracts - used for order-time score lookup
        results = [c for g in groups for c in g["contracts"]]

        return {
            "as_of": dt.datetime.now(ET).isoformat(timespec="seconds"),
            "regime": {"label": regime["label"], "score": regime["score"]},
            "universe_size": len(universe),
            "segment": seg,
            "prefilter": prefilter_info,
            "tickers_scanned": len(scan_list),
            "contracts_scanned": sum(r["scanned"] for r in per_ticker),
            "contracts_kept": len(rows),
            "tickers_with_picks": len(grouped),
            "dropped": dropped,
            "groups": groups,
            "results": results,
            "degraded": [
                {"symbol": r["symbol"], "error": r["error"]}
                for r in per_ticker if r["error"]
            ],
            "stale": any(r["stale"] for r in per_ticker) or bool(regime.get("stale")),
        }

    async def scan(self, refresh: bool = False, sector: Optional[str] = None,
                   theme: Optional[str] = None, dte: Optional[str] = None) -> Dict[str, Any]:
        ttl = self.config.get("settings")["cache_ttls_seconds"]["scan"]
        # result cache is per (segment + DTE); the prefilter cache (inside
        # _scan_now) is keyed by segment only, so it is reused across DTE presets
        cache_key = f"scan:result:{self._seg_key(sector, theme)}|{dte or 'default'}"

        async def _compute() -> Dict[str, Any]:
            return await self._scan_now(sector, theme, dte)

        if refresh:
            result = await _compute()
            self.cache.set(cache_key, result, ttl)
            return result
        fetched = await self.cache.get_or_fetch(cache_key, ttl, _compute)
        result = dict(fetched.data)
        if fetched.stale:
            result["stale"] = True
        return result

    def find_cached(self, occ_symbol: str) -> Optional[Dict[str, Any]]:
        """Most recent scored row for a contract across ANY cached scan
        (default or sector/theme-filtered). Used to journal the score
        snapshot at order time."""
        for result in self.cache.values_with_prefix("scan:result:"):
            for row in result.get("results", []):
                if row.get("occ_symbol") == occ_symbol:
                    return row
        return None
