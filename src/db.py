from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

CompletionSource = Literal["todoist_sync", "post_hoc_log", "mcp_complete"]

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sync_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    sync_token TEXT NOT NULL DEFAULT '*',
    last_synced_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS completion_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    todoist_task_id TEXT,
    content TEXT NOT NULL,
    project_name TEXT NOT NULL,
    labels_json TEXT NOT NULL DEFAULT '[]',
    completed_at TIMESTAMP NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('todoist_sync', 'post_hoc_log', 'mcp_complete')),
    UNIQUE(todoist_task_id, completed_at)
);

CREATE INDEX IF NOT EXISTS idx_completion_log_completed_at
    ON completion_log(completed_at);
"""


def _normalize_timestamp(ts: datetime | str) -> str:
    if isinstance(ts, str):
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    else:
        parsed = ts
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Database:
    """Thin SQLite wrapper for sync state and long-term completion history."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA_SQL)

    def get_sync_state(self) -> tuple[str, str | None]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT sync_token, last_synced_at FROM sync_state WHERE id = 1"
            ).fetchone()
        if row is None:
            return "*", None
        return str(row["sync_token"]), str(row["last_synced_at"])

    def update_sync_state(
        self,
        sync_token: str,
        last_synced_at: datetime | str | None = None,
    ) -> None:
        ts = _normalize_timestamp(
            datetime.now(UTC) if last_synced_at is None else last_synced_at
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sync_state (id, sync_token, last_synced_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    sync_token = excluded.sync_token,
                    last_synced_at = excluded.last_synced_at
                """,
                (sync_token, ts),
            )

    def record_completion(
        self,
        *,
        todoist_task_id: str | None,
        content: str,
        project_name: str,
        labels: list[str] | None = None,
        completed_at: datetime | str | None = None,
        source: CompletionSource,
    ) -> bool:
        ts = _normalize_timestamp(
            datetime.now(UTC) if completed_at is None else completed_at
        )
        labels_json = json.dumps(sorted(set(labels or [])))
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO completion_log (
                    todoist_task_id, content, project_name, labels_json, completed_at, source
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (todoist_task_id, content, project_name, labels_json, ts, source),
            )
            return cur.rowcount == 1

    def get_completion_history(
        self,
        *,
        days: int = 90,
        project: str | None = None,
        allowed_projects: Iterable[str] | None = None,
    ) -> list[dict[str, str]]:
        if not (1 <= days <= 365):
            raise ValueError("days must be between 1 and 365")
        since = _normalize_timestamp(datetime.now(UTC) - timedelta(days=days))
        query = (
            "SELECT content, project_name, completed_at "
            "FROM completion_log WHERE completed_at >= ?"
        )
        params: list[object] = [since]
        if project is not None:
            query += " AND project_name = ?"
            params.append(project)
        elif allowed_projects is not None:
            allowed_list = sorted(set(allowed_projects))
            if not allowed_list:
                return []
            placeholders = ", ".join("?" for _ in allowed_list)
            query += f" AND project_name IN ({placeholders})"
            params.extend(allowed_list)
        query += " ORDER BY completed_at DESC"

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "content": str(row["content"]),
                "project_name": str(row["project_name"]),
                "completed_at": str(row["completed_at"]),
            }
            for row in rows
        ]
