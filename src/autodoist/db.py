from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator, Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from autodoist.models import CompletionHistoryItem, normalize_text

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


@dataclass(frozen=True, slots=True)
class CompletionRecord:
    todoist_task_id: str | None
    content: str
    project_name: str
    labels: list[str] | None = None
    completed_at: datetime | str | None = None
    source: CompletionSource = "todoist_sync"
    timestamp_estimated: bool = False


def parse_utc_timestamp(ts: datetime | str) -> datetime:
    parsed = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def normalize_timestamp(ts: datetime | str) -> str:
    return parse_utc_timestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


class Database:
    """Thin SQLite wrapper for sync state and long-term completion history."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA_SQL)

    def ping(self) -> None:
        with self._connect() as conn:
            conn.execute("SELECT 1")

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
        ts = normalize_timestamp(
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

    def record_completions(self, records: Sequence[CompletionRecord]) -> int:
        if not records:
            return 0
        inserted = 0
        with self._connect() as conn:
            for rec in records:
                if rec.timestamp_estimated and rec.todoist_task_id is not None:
                    already_logged = conn.execute(
                        "SELECT 1 FROM completion_log WHERE todoist_task_id = ? LIMIT 1",
                        (rec.todoist_task_id,),
                    ).fetchone()
                    if already_logged is not None:
                        continue

                dt = parse_utc_timestamp(
                    datetime.now(UTC) if rec.completed_at is None else rec.completed_at
                )
                ts = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                if rec.source == "todoist_sync" and rec.todoist_task_id is not None:
                    low = (dt - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    high = (dt + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    mcp_row = conn.execute(
                        """
                        SELECT id FROM completion_log
                        WHERE todoist_task_id = ?
                          AND source = 'mcp_complete'
                          AND completed_at BETWEEN ? AND ?
                        ORDER BY id ASC
                        LIMIT 1
                        """,
                        (rec.todoist_task_id, low, high),
                    ).fetchone()
                    if mcp_row is not None:
                        existing_exact = conn.execute(
                            """
                            SELECT 1 FROM completion_log
                            WHERE todoist_task_id = ? AND completed_at = ? AND id != ?
                            LIMIT 1
                            """,
                            (rec.todoist_task_id, ts, mcp_row["id"]),
                        ).fetchone()
                        if existing_exact is not None:
                            conn.execute(
                                "DELETE FROM completion_log WHERE id = ?",
                                (mcp_row["id"],),
                            )
                        else:
                            conn.execute(
                                """
                                UPDATE completion_log
                                SET completed_at = ?, source = 'todoist_sync'
                                WHERE id = ?
                                """,
                                (ts, mcp_row["id"]),
                            )
                        continue

                labels_json = json.dumps(sorted(set(rec.labels or [])))
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO completion_log (
                        todoist_task_id, content, project_name, labels_json, completed_at, source
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rec.todoist_task_id,
                        rec.content,
                        rec.project_name,
                        labels_json,
                        ts,
                        rec.source,
                    ),
                )
                if cur.rowcount == 1:
                    inserted += 1
        return inserted

    def has_recent_post_hoc(
        self,
        content: str,
        project_name: str,
        *,
        within_seconds: int = 300,
    ) -> bool:
        target = normalize_text(content)
        since = normalize_timestamp(datetime.now(UTC) - timedelta(seconds=within_seconds))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT content FROM completion_log
                WHERE source = 'post_hoc_log'
                  AND project_name = ?
                  AND completed_at >= ?
                """,
                (project_name, since),
            ).fetchall()
        return any(normalize_text(str(row["content"])) == target for row in rows)

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
        return (
            self.record_completions(
                [
                    CompletionRecord(
                        todoist_task_id=todoist_task_id,
                        content=content,
                        project_name=project_name,
                        labels=labels,
                        completed_at=completed_at,
                        source=source,
                    )
                ]
            )
            == 1
        )

    def get_completion_history(
        self,
        *,
        days: int = 90,
        project: str | None = None,
        allowed_projects: Iterable[str] | None = None,
    ) -> list[CompletionHistoryItem]:
        if not (1 <= days <= 365):
            raise ValueError("days must be between 1 and 365")
        since = normalize_timestamp(datetime.now(UTC) - timedelta(days=days))
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
            CompletionHistoryItem(
                content=str(row["content"]),
                project_name=str(row["project_name"]),
                completed_at=str(row["completed_at"]),
            )
            for row in rows
        ]
