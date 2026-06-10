"""Environment loading. All secrets come from .env at the repo root.

Secret values must never be printed or logged; data.base.redact() scrubs
them from any error text that could reach logs.
"""
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

_loaded = False


def load_env() -> None:
    global _loaded
    if not _loaded:
        load_dotenv(ROOT / ".env", override=False)
        _loaded = True


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    load_env()
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def env_bool(name: str, default: bool = False) -> bool:
    raw = env(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def secret(name: str) -> Optional[str]:
    """Like env(), but treats .env.example placeholders (your_...) as unset."""
    value = env(name)
    if value is None or value.lower().startswith("your_"):
        return None
    return value
