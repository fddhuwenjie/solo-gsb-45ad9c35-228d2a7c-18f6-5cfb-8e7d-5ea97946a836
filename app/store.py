"""SQLite-backed persistence for review batches.

Each batch is stored as a JSON document plus a monotonically increasing
integer version. Writes use conditional SQL statements (compare-and-set on
the version), so concurrent requests carrying the same version succeed at
most once — the database itself arbitrates optimistic concurrency control.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading


class BatchStore:
    """Thread-safe SQLite store for review batches."""

    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS batches (
                    batch_id TEXT PRIMARY KEY,
                    version INTEGER NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )

    @classmethod
    def from_env(cls) -> "BatchStore":
        """Store at ``$BATCH_DB_PATH`` (default: ``./batches.db``)."""
        return cls(os.environ.get("BATCH_DB_PATH", "batches.db"))

    def create(self, batch_id: str, payload: dict) -> bool:
        """Insert a new batch at version 1.

        Returns False when the batch ID already exists; the existing record
        is left untouched.
        """
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO batches (batch_id, version, payload) VALUES (?, 1, ?)",
                    (batch_id, json.dumps(payload, ensure_ascii=False)),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def get(self, batch_id: str) -> dict | None:
        """Current state (payload plus ``version``), or None when unknown."""
        with self._lock:
            row = self._conn.execute(
                "SELECT version, payload FROM batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
        if row is None:
            return None
        version, payload = row
        return {**json.loads(payload), "version": version}

    def update_if_version(
        self, batch_id: str, expected_version: int, payload: dict
    ) -> int | None:
        """Replace the payload and bump the version, but only when the stored
        version still equals ``expected_version``.

        Returns the new version, or None when the compare-and-set failed
        (the caller's version was stale).
        """
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE batches SET version = version + 1, payload = ? "
                "WHERE batch_id = ? AND version = ?",
                (
                    json.dumps(payload, ensure_ascii=False),
                    batch_id,
                    expected_version,
                ),
            )
        if cursor.rowcount == 0:
            return None
        return expected_version + 1

    def close(self) -> None:
        with self._lock:
            self._conn.close()
