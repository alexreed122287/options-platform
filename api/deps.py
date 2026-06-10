"""Singleton wiring: env, hot-reloading config store, cache, clients, engines."""
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from api import db
from data.alpaca_client import AlpacaClient
from data.cache import RateBudget, TTLCache
from data.env import ROOT, env, load_env
from data.fmp_client import FMPClient
from data.public_client import PublicClient
from engine.alerts import AlertLoop
from engine.regime import RegimeEngine
from engine.scoring import Scanner

log = logging.getLogger("api.deps")

CONFIG_DIR = ROOT / "config"


class ConfigStore:
    """Reads config/*.json with mtime-based hot reload, so weights and
    thresholds can be tuned without restarting the server."""

    def __init__(self, directory: Path):
        self.directory = directory
        self._cache: Dict[str, Tuple[float, Any]] = {}

    def get(self, name: str) -> Dict[str, Any]:
        path = self.directory / f"{name}.json"
        mtime = path.stat().st_mtime
        cached = self._cache.get(name)
        if cached and cached[0] == mtime:
            return cached[1]
        data = json.loads(path.read_text())
        self._cache[name] = (mtime, data)
        log.info("loaded config/%s.json", name)
        return data


class Deps:
    def __init__(self) -> None:
        load_env()
        self.config = ConfigStore(CONFIG_DIR)
        settings = self.config.get("settings")
        ttls = settings["cache_ttls_seconds"]
        budgets = settings["rate_budgets"]
        self.cache = TTLCache()
        self.fmp = FMPClient(self.cache, RateBudget("fmp", **budgets["fmp"]), ttls)
        self.alpaca = AlpacaClient(
            self.cache,
            RateBudget("alpaca_trading", **budgets["alpaca_trading"]),
            RateBudget("alpaca_data", **budgets["alpaca_data"]),
            ttls,
        )
        self.public = PublicClient(
            self.cache,
            RateBudget("public", **budgets["public"]),
            ttls,
            settings.get("public", {}),
        )

        # Broker = who holds the account and executes orders.
        # Data source = who feeds the scanner (chains/greeks/spot).
        # Public is ALWAYS real money (no paper environment exists there).
        self.broker_name = (env("BROKER", "alpaca") or "alpaca").lower()
        if self.broker_name not in ("alpaca", "public"):
            log.warning("unknown BROKER=%s, falling back to alpaca", self.broker_name)
            self.broker_name = "alpaca"
        self.broker = self.public if self.broker_name == "public" else self.alpaca

        self.data_source_name = (env("DATA_SOURCE", "alpaca") or "alpaca").lower()
        if self.data_source_name not in ("alpaca", "public"):
            log.warning("unknown DATA_SOURCE=%s, falling back to alpaca", self.data_source_name)
            self.data_source_name = "alpaca"
        self.market_data = self.public if self.data_source_name == "public" else self.alpaca

        db.init_db()
        self.regime = RegimeEngine(self.fmp, self.alpaca, self.config, self.cache)
        self.scanner = Scanner(self.fmp, self.market_data, self.regime, self.config, self.cache)
        self.alerts = AlertLoop(self.scanner, self.market_data, self.config)

    async def aclose(self) -> None:
        await self.fmp.aclose()
        await self.alpaca.aclose()
        await self.public.aclose()


_deps: Optional[Deps] = None


def get_deps() -> Deps:
    global _deps
    if _deps is None:
        _deps = Deps()
    return _deps
