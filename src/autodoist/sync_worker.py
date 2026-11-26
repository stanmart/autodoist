from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from autodoist.config import load_config, validate_todoist_token
from autodoist.db import CompletionRecord, Database, normalize_timestamp, parse_utc_timestamp
from autodoist.todoist import TodoistClient

logger = logging.getLogger(__name__)


async def run_sync(
    db: Database,
    todoist: TodoistClient,
    allowed_projects: Iterable[str],
) -> dict[str, Any]:
    """Perform incremental /sync + best-effort activity-log pull into completion_log."""
    allowed_set = set(allowed_projects)
    sync_token, last_synced_at = await asyncio.to_thread(db.get_sync_state)

    await todoist.refresh_projects(force=True)
    new_token, items = await todoist.sync_incremental(sync_token)

    now = datetime.now(UTC)
    now_iso = normalize_timestamp(now)
    records: list[CompletionRecord] = []

    for item in items:
        if item.is_deleted or not item.checked:
            continue
        project_name = todoist.get_project_name(item.project_id)
        if project_name not in allowed_set:
            continue
        records.append(
            CompletionRecord(
                todoist_task_id=item.id,
                content=item.content,
                project_name=project_name,
                labels=item.labels,
                completed_at=item.completed_at or now_iso,
                source="todoist_sync",
            )
        )

    # Best-effort activity-log pull (captures recurring completions when supported by plan)
    lookback_limit = now - timedelta(days=90)
    if last_synced_at:
        since_dt = max(parse_utc_timestamp(last_synced_at) - timedelta(hours=1), lookback_limit)
    else:
        since_dt = now - timedelta(days=7)

    activities_endpoint_ok = True
    try:
        events = await todoist.get_completed_activities(normalize_timestamp(since_dt), now_iso)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (402, 403):
            logger.warning(
                "Todoist activities endpoint returned %s; skipping recurring pull",
                exc.response.status_code,
            )
            activities_endpoint_ok = False
            events = []
        else:
            raise

    for event in events:
        project_name = todoist.get_project_name(event.project_id)
        if project_name not in allowed_set:
            continue
        records.append(
            CompletionRecord(
                todoist_task_id=event.task_id,
                content=event.content,
                project_name=project_name,
                labels=[],
                completed_at=event.completed_at or now_iso,
                source="todoist_sync",
            )
        )

    inserted = await asyncio.to_thread(db.record_completions, records)
    await asyncio.to_thread(db.update_sync_state, new_token, now)

    return {
        "inserted_completions": inserted,
        "sync_token_updated": new_token != sync_token,
        "activities_endpoint_ok": activities_endpoint_ok,
    }


async def sync_loop(
    db: Database,
    todoist: TodoistClient,
    allowed_projects: Iterable[str],
    interval_seconds: float,
) -> None:
    """Run incremental sync immediately and then periodically until cancelled."""
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    projects = tuple(allowed_projects)
    while True:
        try:
            summary = await run_sync(db, todoist, projects)
            logger.info("Periodic sync completed: %s", summary)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Periodic sync failed; will retry on next interval")
        await asyncio.sleep(interval_seconds)


async def _run_cli() -> None:
    app_config, env = load_config()
    validate_todoist_token(env)
    db = Database(app_config.storage.db_path)
    db.init_db()
    todoist = TodoistClient(env.todoist_api_token)
    try:
        summary = await run_sync(db, todoist, app_config.rules.allowed_projects)
    finally:
        await todoist.aclose()
    print(json.dumps(summary))


def main() -> None:
    asyncio.run(_run_cli())


if __name__ == "__main__":
    main()
