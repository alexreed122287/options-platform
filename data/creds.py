"""Per-request credential context for multi-user (bring-your-own-keys) mode.

In single-user mode (no creds context set), cred()/cred_bool() are exactly
secret()/env() - keys come from the server's .env, behavior is unchanged.

In multi-user mode the middleware sets a per-request creds bundle (decoded
from the X-Creds header the browser sends). Then:
  - SECRET keys come ONLY from that bundle. They NEVER fall back to the
    server's .env, so one visitor can't accidentally use another's (or the
    operator's) keys.
  - config values (BROKER, DATA_SOURCE, ...) come from the bundle, else the
    server default.
The bundle lives only for the duration of the request (a contextvar, which is
isolated per async task) and is never written to disk.
"""
import contextvars
import hashlib
from typing import Any, Dict, Optional

from .env import env, secret

# Secret keys: in BYOK mode these come ONLY from the per-request bundle.
SECRET_KEYS = (
    "FMP_API_KEY", "ALPACA_API_KEY", "ALPACA_SECRET_KEY",
    "TRADIER_ACCESS_TOKEN", "PUBLIC_API_SECRET",
)
# Per-user config that may also travel in the bundle (else server default).
CONFIG_KEYS = (
    "BROKER", "DATA_SOURCE", "TRADIER_ENV", "ALPACA_PAPER",
    "LIVE_TRADING_ENABLED", "TRADIER_ACCOUNT_ID", "PUBLIC_ACCOUNT_ID",
)

_creds: contextvars.ContextVar = contextvars.ContextVar("creds", default=None)


def set_creds(bundle: Optional[Dict[str, Any]]) -> contextvars.Token:
    return _creds.set(bundle)


def reset_creds(token: contextvars.Token) -> None:
    _creds.reset(token)


def creds_active() -> bool:
    return _creds.get() is not None


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower().startswith("your_"):
        return None
    return s


def cred(name: str) -> Optional[str]:
    """Resolve a credential/config value for the current request."""
    bundle = _creds.get()
    if bundle is None:
        # single-user: straight from the server environment
        return secret(name) if name in SECRET_KEYS else env(name)
    value = _clean(bundle.get(name))
    if value is not None:
        return value
    if name in SECRET_KEYS:
        return None              # never fall back to the server's secret keys
    return env(name)             # config can use the server default


def cred_bool(name: str, default: bool = False) -> bool:
    value = cred(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")


def user_id() -> str:
    """Stable id for the current credential set, for namespacing cache + DB.
    'local' in single-user mode; a short hash of the secret keys otherwise."""
    bundle = _creds.get()
    if bundle is None:
        return "local"
    basis = "|".join(f"{k}={_clean(bundle.get(k)) or ''}" for k in SECRET_KEYS)
    return "u_" + hashlib.sha256(basis.encode()).hexdigest()[:16]
