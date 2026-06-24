"""Timer routes: server-side storage for the line/task stopwatch so the same
capture history is shared across every device (phone, iPad, desktop).

Records are keyed by a client-generated id (a uuid the browser creates) so the
frontend can keep working offline and sync its queue later with no collisions -
re-posting the same record is idempotent (INSERT OR IGNORE).

These sit behind the same optional DASHBOARD_TOKEN gate as every other route.
"""
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter
from pydantic import BaseModel, Field

from api import db

router = APIRouter()


class Capture(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    task: str = Field(min_length=1, max_length=200)
    durationMs: int = Field(ge=0)
    capturedCentral: str = Field(max_length=64)
    capturedISO: str = Field(max_length=40)


class SyncRequest(BaseModel):
    # records created on this device while offline (or just now)
    captures: List[Capture] = Field(default_factory=list)
    # ids deleted on this device while offline - applied server-side so a
    # delete isn't undone by the next pull
    deletedIds: List[str] = Field(default_factory=list)


def _row_to_capture(row: dict) -> dict:
    return {
        "id": row["id"],
        "task": row["task"],
        "durationMs": row["duration_ms"],
        "capturedCentral": row["captured_central"],
        "capturedISO": row["captured_iso"],
    }


def _all_captures() -> List[dict]:
    rows = db.query(
        "SELECT id, task, duration_ms, captured_central, captured_iso "
        "FROM timer_captures ORDER BY captured_iso DESC"
    )
    return [_row_to_capture(r) for r in rows]


@router.get("/timer/captures")
def list_captures():
    return {"captures": _all_captures()}


@router.post("/timer/captures")
def add_capture(cap: Capture):
    """Upsert a single capture (used right after each Stop)."""
    now = datetime.now(timezone.utc).isoformat()
    db.execute_rc(
        "INSERT OR IGNORE INTO timer_captures "
        "(id, task, duration_ms, captured_central, captured_iso, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (cap.id, cap.task, cap.durationMs, cap.capturedCentral, cap.capturedISO, now),
    )
    return {"ok": True}


@router.post("/timer/sync")
def sync(req: SyncRequest):
    """Push local changes (new captures + offline deletes), then return the
    full authoritative list so the client can replace its cache."""
    now = datetime.now(timezone.utc).isoformat()
    for cap in req.captures:
        db.execute_rc(
            "INSERT OR IGNORE INTO timer_captures "
            "(id, task, duration_ms, captured_central, captured_iso, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (cap.id, cap.task, cap.durationMs, cap.capturedCentral,
             cap.capturedISO, now),
        )
    for cap_id in req.deletedIds:
        db.execute("DELETE FROM timer_captures WHERE id = ?", (cap_id,))
    return {"captures": _all_captures()}


@router.delete("/timer/captures/{capture_id}")
def delete_capture(capture_id: str):
    db.execute("DELETE FROM timer_captures WHERE id = ?", (capture_id,))
    return {"ok": True}


@router.delete("/timer/captures")
def clear_captures():
    db.execute("DELETE FROM timer_captures")
    return {"ok": True}
