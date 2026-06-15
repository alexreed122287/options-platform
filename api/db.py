"""SQLite persistence. One short-lived connection per call, WAL mode."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List

from data.env import ROOT, env

SCHEMA = """
CREATE TABLE IF NOT EXISTS regime_snapshots (
  date TEXT PRIMARY KEY,
  score REAL NOT NULL,
  label TEXT NOT NULL,
  components TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS iv_history (
  symbol TEXT NOT NULL,
  date TEXT NOT NULL,
  atm_iv REAL NOT NULL,
  PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS watchlist (
  symbol TEXT PRIMARY KEY,
  added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS journal (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  order_id TEXT,
  occ_symbol TEXT NOT NULL,
  underlying TEXT,
  side TEXT NOT NULL DEFAULT 'buy',
  qty INTEGER NOT NULL,
  limit_price REAL,
  status TEXT,
  filled_avg_price REAL,
  regime_label TEXT,
  regime_score REAL,
  score_total REAL,
  score_breakdown TEXT,
  notes TEXT NOT NULL DEFAULT '',
  closed_at TEXT,
  exit_price REAL,
  realized_pnl REAL,
  paper INTEGER NOT NULL DEFAULT 1,
  broker TEXT NOT NULL DEFAULT 'alpaca'
);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  date TEXT NOT NULL,
  occ_symbol TEXT NOT NULL,
  underlying TEXT,
  score REAL NOT NULL,
  payload TEXT,
  seen INTEGER NOT NULL DEFAULT 0,
  UNIQUE (occ_symbol, date)
);
CREATE TABLE IF NOT EXISTS score_tracking (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_date TEXT NOT NULL,
  snapshot_at TEXT NOT NULL,
  occ_symbol TEXT NOT NULL,
  underlying TEXT,
  expiration TEXT,
  dte INTEGER,
  delta REAL,
  score REAL NOT NULL,
  entry_mid REAL,
  entry_spot REAL,
  updated_at TEXT,
  current_mid REAL,
  current_spot REAL,
  option_return_pct REAL,
  underlying_return_pct REAL,
  days_held REAL,
  status TEXT NOT NULL DEFAULT 'open',
  UNIQUE (occ_symbol, snapshot_date)
);
"""


def db_path() -> Path:
    configured = env("DB_PATH")
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else ROOT / path
    return ROOT / "options_platform.db"


@contextmanager
def connect():
    conn = sqlite3.connect(db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        # migrations for databases created before a column existed
        journal_cols = {row["name"] for row in conn.execute("PRAGMA table_info(journal)")}
        if "broker" not in journal_cols:
            conn.execute(
                "ALTER TABLE journal ADD COLUMN broker TEXT NOT NULL DEFAULT 'alpaca'"
            )


def query(sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
    with connect() as conn:
        return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    """Run a write statement; returns lastrowid (0 for non-inserts)."""
    with connect() as conn:
        cur = conn.execute(sql, tuple(params))
        return cur.lastrowid or 0


def execute_rc(sql: str, params: Iterable[Any] = ()) -> int:
    """Run a write statement; returns affected rowcount (0 when an
    INSERT OR IGNORE was ignored)."""
    with connect() as conn:
        cur = conn.execute(sql, tuple(params))
        return cur.rowcount if cur.rowcount > 0 else 0
