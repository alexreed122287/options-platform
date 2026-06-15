"""Settings routes: inspect and update provider keys + broker/data-source
selection, and the scoring weights, from the dashboard.

Security model:
  - All /api routes already sit behind the optional DASHBOARD_TOKEN gate.
  - Secret values are NEVER returned - GET reports only whether each key is set.
  - Only an allowlisted set of env vars can be written; values are sanitized
    (no newlines/control chars) and written to the local .env, which is
    gitignored. New secrets are registered with the log redactor.
  - Demo mode has no backend, so the dashboard's Settings tab is read-only there.
"""
import json
import logging
import threading
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.deps import CONFIG_DIR, get_deps
from data.base import register_secret
from data.env import ROOT, env, env_bool

WEIGHT_KEYS = ("delta_fit", "extrinsic", "spread", "open_interest",
               "volume", "iv_rank", "dte_fit", "trend_alignment")

log = logging.getLogger("api.settings")

router = APIRouter()
_env_lock = threading.Lock()
ENV_PATH = ROOT / ".env"

# Secret keys: status only, values never leave the server.
SECRET_KEYS = (
    "FMP_API_KEY", "ALPACA_API_KEY", "ALPACA_SECRET_KEY",
    "TRADIER_ACCESS_TOKEN", "PUBLIC_API_SECRET",
)
# Non-secret config: values may be shown and edited.
ENUM_KEYS = {
    "BROKER": ("alpaca", "tradier", "public"),
    "DATA_SOURCE": ("alpaca", "tradier", "public"),
    "TRADIER_ENV": ("production", "sandbox"),
    "ALPACA_PAPER": ("true", "false"),
    "LIVE_TRADING_ENABLED": ("true", "false"),
}
PLAIN_KEYS = ("TRADIER_ACCOUNT_ID", "PUBLIC_ACCOUNT_ID")
WRITABLE = set(SECRET_KEYS) | set(ENUM_KEYS) | set(PLAIN_KEYS)


def _is_placeholder(value: Optional[str]) -> bool:
    return bool(value) and value.lower().startswith("your_")


def _sanitize(key: str, value: str) -> str:
    value = value.strip()
    if any(ch in value for ch in ("\n", "\r", "\x00")):
        raise HTTPException(status_code=400, detail=f"{key}: value contains illegal characters")
    if len(value) > 256:
        raise HTTPException(status_code=400, detail=f"{key}: value too long")
    if key in ENUM_KEYS and value and value.lower() not in ENUM_KEYS[key]:
        raise HTTPException(
            status_code=400,
            detail=f"{key} must be one of {', '.join(ENUM_KEYS[key])}",
        )
    return value


def _write_env(updates: Dict[str, str]) -> None:
    """Upsert keys into .env (empty value removes the line) and mirror into
    os.environ so the change applies live."""
    with _env_lock:
        lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
        remaining = dict(updates)
        out = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in remaining:
                    value = remaining.pop(key)
                    if value == "":
                        continue  # clearing: drop the line
                    out.append(f"{key}={value}")
                    continue
            out.append(line)
        for key, value in remaining.items():
            if value != "":
                out.append(f"{key}={value}")
        ENV_PATH.write_text("\n".join(out) + "\n")

    import os
    for key, value in updates.items():
        if value == "":
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
            if key in SECRET_KEYS:
                register_secret(value)


def _snapshot() -> Dict[str, Any]:
    deps = get_deps()
    keys = {k: {"set": bool(env(k)) and not _is_placeholder(env(k))} for k in SECRET_KEYS}
    values = {k: env(k) for k in PLAIN_KEYS}
    return {
        "broker": deps.broker_name,
        "data_source": deps.data_source_name,
        "alpaca_paper": env_bool("ALPACA_PAPER", True),
        "tradier_env": (env("TRADIER_ENV", "production") or "production").lower(),
        "live_trading_enabled": env_bool("LIVE_TRADING_ENABLED", False),
        "keys": keys,
        "values": values,
        "brokers": ["alpaca", "tradier", "public"],
        "real_money": {
            "alpaca": not deps.alpaca.paper,
            "tradier": not deps.tradier.paper,
            "public": True,
        },
    }


class SettingsUpdate(BaseModel):
    keys: Dict[str, str] = {}          # secret keys to set ("" clears)
    broker: Optional[str] = None
    data_source: Optional[str] = None
    alpaca_paper: Optional[bool] = None
    tradier_env: Optional[str] = None
    tradier_account_id: Optional[str] = None
    public_account_id: Optional[str] = None
    live_trading_enabled: Optional[bool] = None


@router.get("/settings")
async def get_settings() -> Dict[str, Any]:
    return _snapshot()


@router.post("/settings")
async def update_settings(req: SettingsUpdate) -> Dict[str, Any]:
    updates: Dict[str, str] = {}

    for key, raw in (req.keys or {}).items():
        if key not in SECRET_KEYS:
            raise HTTPException(status_code=400, detail=f"{key} is not an editable secret")
        updates[key] = _sanitize(key, raw)

    if req.broker is not None:
        updates["BROKER"] = _sanitize("BROKER", req.broker)
    if req.data_source is not None:
        updates["DATA_SOURCE"] = _sanitize("DATA_SOURCE", req.data_source)
    if req.tradier_env is not None:
        updates["TRADIER_ENV"] = _sanitize("TRADIER_ENV", req.tradier_env)
    if req.tradier_account_id is not None:
        updates["TRADIER_ACCOUNT_ID"] = _sanitize("TRADIER_ACCOUNT_ID", req.tradier_account_id)
    if req.public_account_id is not None:
        updates["PUBLIC_ACCOUNT_ID"] = _sanitize("PUBLIC_ACCOUNT_ID", req.public_account_id)
    if req.alpaca_paper is not None:
        updates["ALPACA_PAPER"] = "true" if req.alpaca_paper else "false"
    if req.live_trading_enabled is not None:
        updates["LIVE_TRADING_ENABLED"] = "true" if req.live_trading_enabled else "false"

    if not updates:
        raise HTTPException(status_code=400, detail="no settings provided")

    _write_env(updates)
    # Tradier caches its discovered account id; clear it if the token/env/id changed.
    deps = get_deps()
    if any(k in updates for k in ("TRADIER_ACCESS_TOKEN", "TRADIER_ENV", "TRADIER_ACCOUNT_ID")):
        deps.tradier._account_id_cached = None
    if "PUBLIC_API_SECRET" in updates:
        deps.public._invalidate_token()
        deps.public._account_id_cached = None
    deps.reconfigure()
    log.info("settings updated: %s", ", ".join(sorted(updates)))
    return {"ok": True, "applied": sorted(updates), **_snapshot()}


# ----------------------------------------------------- scoring weights

class ScoringUpdate(BaseModel):
    weights: Dict[str, float] = {}
    delta_min: Optional[float] = None
    delta_max: Optional[float] = None


@router.get("/scoring-config")
async def get_scoring_config() -> Dict[str, Any]:
    cfg = get_deps().config.get("scoring")
    band = cfg.get("delta_band", {})
    return {
        "weights": {k: float(cfg.get("weights", {}).get(k, 0)) for k in WEIGHT_KEYS},
        "delta_band": {"min": band.get("min"), "max": band.get("max")},
        "defaults": {
            "weights": {"delta_fit": 0.2, "extrinsic": 0.15, "spread": 0.15,
                        "open_interest": 0.1, "volume": 0.1, "iv_rank": 0.1,
                        "dte_fit": 0.1, "trend_alignment": 0.1},
            "delta_band": {"min": 0.6, "max": 0.8},
        },
    }


@router.post("/scoring-config")
async def update_scoring_config(req: ScoringUpdate) -> Dict[str, Any]:
    path = CONFIG_DIR / "scoring.json"
    cfg = json.loads(path.read_text())

    if req.weights:
        for key, val in req.weights.items():
            if key not in WEIGHT_KEYS:
                raise HTTPException(status_code=400, detail=f"unknown weight: {key}")
            if not isinstance(val, (int, float)) or val < 0 or val > 1000:
                raise HTTPException(status_code=400, detail=f"{key}: weight must be 0..1000")
        merged = {**cfg.get("weights", {}), **{k: float(v) for k, v in req.weights.items()}}
        if sum(merged.get(k, 0) for k in WEIGHT_KEYS) <= 0:
            raise HTTPException(status_code=400, detail="at least one weight must be > 0")
        cfg["weights"] = merged

    if req.delta_min is not None or req.delta_max is not None:
        band = dict(cfg.get("delta_band", {}))
        lo = req.delta_min if req.delta_min is not None else band.get("min")
        hi = req.delta_max if req.delta_max is not None else band.get("max")
        if not (0 < lo <= hi <= 1):
            raise HTTPException(status_code=400, detail="delta band must satisfy 0 < min <= max <= 1")
        band["min"], band["max"] = float(lo), float(hi)
        cfg["delta_band"] = band

    with _env_lock:  # reuse the file lock for atomic-ish writes
        path.write_text(json.dumps(cfg, indent=2) + "\n")
    # scoring.json is mtime hot-reloaded; drop cached scans so a re-scan re-scores
    get_deps().cache.invalidate_prefix("scan:result:")
    log.info("scoring config updated")
    return {"ok": True, **(await get_scoring_config())}
