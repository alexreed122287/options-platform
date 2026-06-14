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
from data.tradier_client import TradierClient
from engine.alerts import AlertLoop
from engine.regime import RegimeEngine
from engine.scoring import Scanner

log = logging.getLogger("api.deps")

CONFIG_DIR = ROOT / "config"

BROKERS = ("alpaca", "tradier", "public")


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
        self.tradier = TradierClient(
            self.cache, RateBudget("tradier", **budgets["tradier"]), ttls
        )
        self._clients = {
            "alpaca": self.alpaca, "tradier": self.tradier, "public": self.public,
        }

        db.init_db()
        self.regime = RegimeEngine(self.fmp, self.alpaca, self.config, self.cache)
        # broker = who executes orders / holds the account
        # data source = who feeds the scanner (chains/greeks/spot)
        self.scanner = Scanner(self.fmp, self.alpaca, self.regime, self.config, self.cache)
        self.alerts = AlertLoop(self.scanner, self.alpaca, self.config)
        self.reconfigure()

    def reconfigure(self) -> None:
        """(Re)resolve broker and data source from env and repoint the engines.
        Called at startup and after a Settings change so key/broker/data-source
        updates apply live without a restart. Tradier production and Public are
        real money; only Alpaca paper and Tradier sandbox are paper."""
        self.broker_name = (env("BROKER", "alpaca") or "alpaca").lower()
        if self.broker_name not in BROKERS:
            log.warning("unknown BROKER=%s, falling back to alpaca", self.broker_name)
            self.broker_name = "alpaca"
        self.broker = self._clients[self.broker_name]

        self.data_source_name = (env("DATA_SOURCE", "alpaca") or "alpaca").lower()
        if self.data_source_name not in BROKERS:
            log.warning("unknown DATA_SOURCE=%s, falling back to alpaca", self.data_source_name)
            self.data_source_name = "alpaca"
        self.market_data = self._clients[self.data_source_name]

        # repoint the scanner/alert loop at the active data source, then drop
        # cached scan/regime so the next read reflects the new source
        self.scanner.market_data = self.market_data
        self.alerts.market_data = self.market_data
        self.cache.invalidate_prefix("scan:result:")
        self.cache.invalidate_prefix("scan:prefilter:")
        self.cache.invalidate("regime:result")

    async def aclose(self) -> None:
        await self.fmp.aclose()
        await self.alpaca.aclose()
        await self.public.aclose()
        await self.tradier.aclose()


_deps: Optional[Deps] = None


def get_deps() -> Deps:
    global _deps
    if _deps is None:
        _deps = Deps()
    return _deps
