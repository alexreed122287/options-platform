"""Singleton wiring: env, hot-reloading config store, cache, clients, engines."""
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from api import db
from data.alpaca_client import AlpacaClient
from data.cache import RateBudget, TTLCache
from data.env import ROOT, load_env
from data.fmp_client import FMPClient
from engine.regime import RegimeEngine

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
        db.init_db()
        self.regime = RegimeEngine(self.fmp, self.alpaca, self.config, self.cache)

    async def aclose(self) -> None:
        await self.fmp.aclose()
        await self.alpaca.aclose()


_deps: Optional[Deps] = None


def get_deps() -> Deps:
    global _deps
    if _deps is None:
        _deps = Deps()
    return _deps
