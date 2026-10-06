"""Lightweight usage analytics stored in SQLite."""

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("ANALYTICS_DB_PATH", "data/analytics.db"))

# Events the browser is allowed to report; everything else is recorded server-side.
CLIENT_EVENTS = {
    "page_view",
    "file_selected",
    "file_rejected",
    "convert_another",
}

_lock = threading.Lock()


@contextmanager
def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    with _lock, _connect() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                username TEXT,
                event TEXT NOT NULL,
                details TEXT
            )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts)")


def track(event: str, username: str | None = None, **details) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = json.dumps(details) if details else None
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO events (ts, username, event, details) VALUES (?, ?, ?, ?)",
            (ts, username, event, payload),
        )


def summary(days: int = 7, recent_limit: int = 100) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with _lock, _connect() as conn:
        totals = conn.execute(
            "SELECT event, COUNT(*) AS n FROM events WHERE ts >= ? GROUP BY event ORDER BY n DESC",
            (since,),
        ).fetchall()
        per_user = conn.execute(
            """SELECT username,
                      SUM(event = 'conversion_succeeded') AS conversions,
                      SUM(event = 'conversion_failed') AS failures,
                      SUM(event = 'jpeg_downloaded') AS downloads,
                      MAX(ts) AS last_seen
               FROM events WHERE ts >= ? AND username IS NOT NULL
               GROUP BY username ORDER BY conversions DESC""",
            (since,),
        ).fetchall()
        daily = conn.execute(
            """SELECT substr(ts, 1, 10) AS day,
                      SUM(event = 'conversion_succeeded') AS conversions,
                      SUM(event = 'conversion_failed') AS failures
               FROM events WHERE ts >= ? GROUP BY day ORDER BY day""",
            (since,),
        ).fetchall()
        recent = conn.execute(
            "SELECT ts, username, event, details FROM events ORDER BY id DESC LIMIT ?",
            (recent_limit,),
        ).fetchall()

    counts = {row["event"]: row["n"] for row in totals}
    succeeded = counts.get("conversion_succeeded", 0)
    failed = counts.get("conversion_failed", 0)
    return {
        "days": days,
        "counts": counts,
        "conversions": succeeded,
        "failures": failed,
        "success_rate": round(100 * succeeded / (succeeded + failed)) if succeeded + failed else None,
        "per_user": [dict(r) for r in per_user],
        "daily": [dict(r) for r in daily],
        "recent": [dict(r) for r in recent],
    }
