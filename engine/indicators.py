"""Pure indicator math. No I/O, fully deterministic."""
from typing import Any, Dict, List, Optional, Sequence


def ema(values: Sequence[float], period: int) -> Optional[List[float]]:
    """EMA series seeded with the SMA of the first `period` values.
    Returns None when there is not enough data."""
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    out = [sum(values[:period]) / period]
    for value in values[period:]:
        out.append(value * k + out[-1] * (1.0 - k))
    return out


def ema_last(values: Sequence[float], period: int) -> Optional[float]:
    series = ema(values, period)
    return series[-1] if series else None


def trend_structure(closes: Sequence[float]) -> Dict[str, Any]:
    """0..1 trend score from 10/20/50 EMA structure.

    Four equally weighted checks: price above EMA10, EMA10 above EMA20,
    EMA20 above EMA50, EMA50 rising vs 5 bars ago. Checks that cannot be
    computed (not enough history) are excluded from the denominator.
    """
    if not closes:
        return {"score": 0.5, "ok": False, "detail": {"reason": "no price history"}}
    price = closes[-1]
    e10 = ema_last(closes, 10)
    e20 = ema_last(closes, 20)
    e50_series = ema(closes, 50)
    e50 = e50_series[-1] if e50_series else None

    points = 0.0
    checks = 0
    if e10 is not None:
        checks += 1
        points += 1.0 if price > e10 else 0.0
    if e10 is not None and e20 is not None:
        checks += 1
        points += 1.0 if e10 > e20 else 0.0
    if e20 is not None and e50 is not None:
        checks += 1
        points += 1.0 if e20 > e50 else 0.0
    e50_rising = None
    if e50_series is not None and len(e50_series) > 5:
        e50_rising = e50_series[-1] > e50_series[-6]
        checks += 1
        points += 1.0 if e50_rising else 0.0

    score = points / checks if checks else 0.5
    return {
        "score": round(score, 3),
        "ok": checks == 4,
        "detail": {
            "price": round(price, 2),
            "ema10": round(e10, 2) if e10 is not None else None,
            "ema20": round(e20, 2) if e20 is not None else None,
            "ema50": round(e50, 2) if e50 is not None else None,
            "ema50_rising": e50_rising,
            "checks_used": checks,
        },
    }


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))
