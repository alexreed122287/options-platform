"""Shared HTTP plumbing for provider clients: redaction, errors, health status."""
import logging
import re
import time
from typing import Any, Dict, Optional

import httpx

from .cache import RateBudget, TTLCache
from .env import env

log = logging.getLogger("data.http")

_SECRET_ENV_NAMES = ("FMP_API_KEY", "ALPACA_API_KEY", "ALPACA_SECRET_KEY")
_APIKEY_RE = re.compile(r"(apikey=)[^&\s\"']+", re.IGNORECASE)


def redact(text: str) -> str:
    """Scrub API keys from any string destined for logs or error messages."""
    if not text:
        return text
    out = _APIKEY_RE.sub(r"\1***", text)
    for name in _SECRET_ENV_NAMES:
        value = env(name)
        if value and value in out:
            out = out.replace(value, "***")
    return out


class ProviderError(Exception):
    """A provider call failed. The message is always redacted."""


class BaseClient:
    name = "base"

    def __init__(self, cache: TTLCache, budget: RateBudget, timeout: float = 20.0):
        self.cache = cache
        self.budget = budget
        self._http = httpx.AsyncClient(timeout=timeout)
        self.last_success: Optional[float] = None
        self.last_error: Optional[float] = None
        self.last_error_msg: Optional[str] = None

    @property
    def configured(self) -> bool:
        return True

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request_json(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        budget: Optional[RateBudget] = None,
    ) -> Any:
        (budget or self.budget).record()
        try:
            resp = await self._http.request(
                method, url, params=params, headers=headers, json=json_body
            )
        except httpx.HTTPError as exc:
            msg = redact(f"{self.name}: {type(exc).__name__}: {exc}")
            self._note_error(msg)
            raise ProviderError(msg) from exc
        if resp.status_code >= 400:
            msg = redact(
                f"{self.name} HTTP {resp.status_code} {resp.request.url.path}: {resp.text[:300]}"
            )
            self._note_error(msg)
            raise ProviderError(msg)
        self.last_success = time.time()
        try:
            return resp.json()
        except ValueError as exc:
            msg = f"{self.name}: invalid JSON from {resp.request.url.path}"
            self._note_error(msg)
            raise ProviderError(msg) from exc

    def _note_error(self, msg: str) -> None:
        self.last_error = time.time()
        self.last_error_msg = redact(msg)
        log.warning("%s", self.last_error_msg)

    def status(self) -> Dict[str, Any]:
        now = time.time()
        return {
            "name": self.name,
            "configured": self.configured,
            "last_success_age_s": round(now - self.last_success, 1) if self.last_success else None,
            "last_error_age_s": round(now - self.last_error, 1) if self.last_error else None,
            "last_error": self.last_error_msg,
        }
