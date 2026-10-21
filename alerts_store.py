"""
Which meeting alerts have gone off, so none goes off twice -- not even
after a restart. Kept in alerts.db next to the boards' kanban.db (a file of
its own, so the alerter never writes to the app's database).

Each alert is keyed by the event's UID plus its start time: occurrences of
a recurring meeting share a UID, and a meeting moved to a new time gets a
new key, so it's announced again at the new time.
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import database as db

STORE_PATH = db.DATA_DIR / "alerts.db"
KEEP_FOR = timedelta(days=2)  # records of meetings that started longer ago than this are pruned


class AlertStore:
    def __init__(self, path: Path = STORE_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS announced (
                   uid       TEXT NOT NULL,
                   start_utc TEXT NOT NULL,  -- the occurrence's start, ISO format, UTC
                   title     TEXT NOT NULL,
                   announced_at TEXT NOT NULL,
                   PRIMARY KEY (uid, start_utc)
               )"""
        )
        self._conn.commit()

    def was_announced(self, key: tuple[str, str]) -> bool:
        row = self._conn.execute("SELECT 1 FROM announced WHERE uid = ? AND start_utc = ?", key).fetchone()
        return row is not None

    def mark_announced(self, key: tuple[str, str], title: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO announced (uid, start_utc, title, announced_at) VALUES (?, ?, ?, ?)",
            (*key, title, datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()

    def prune(self, now: datetime | None = None) -> None:
        cutoff = (now or datetime.now(timezone.utc)) - KEEP_FOR
        cutoff = cutoff.astimezone(timezone.utc).replace(microsecond=0).isoformat()
        # ISO times in UTC sort as text in time order.
        self._conn.execute("DELETE FROM announced WHERE start_utc < ?", (cutoff,))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
