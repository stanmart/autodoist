from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from src.db import Database
from src.todoist import TodoistClient

logger = logging.getLogger(__name__)


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_utc(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


async def run_sync(
    db: Database,
    todoist: TodoistClient,
    allowed_projects: Iterable[str],
) -> dict[str, Any]:
    """Perform incremental /sync + best-effort completed-tasks fetch into completion_log."""
    allowed_set = set(allowed_projects)
    sync_token, last_synced_at = db.get_sync_state()

    sync_data = await todoist.sync_incremental(sync_token)
    await todoist.ensure_projects_loaded()

    now = datetime.now(UTC)
    now_iso = _iso_utc(now)
    inserted = 0

    for item in sync_data.get("items", []):
        if item.get("is_deleted") or not item.get("checked"):
            continue
        project_name = todoist.get_project_name(str(item.get("project_id", "")))
        if project_name not in allowed_set:
            continue
        completed_at = item.get("completed_at") or now_iso
        was_inserted = db.record_completion(
            todoist_task_id=str(item["id"]),
            content=str(item.get("content", "")),
            project_name=project_name,
            labels=[str(lbl) for lbl in item.get("labels", [])],
            completed_at=completed_at,
            source="todoist_sync",
        )
        if was_inserted:
            inserted += 1

    # Best-effort completed-tasks pull (captures recurring completions when supported by plan)
    seven_days_ago = now - timedelta(days=7)
    if last_synced_at:
        since_dt = max(_parse_iso_utc(last_synced_at) - timedelta(hours=1), seven_days_ago)
    else:
        since_dt = seven_days_ago

    completed_endpoint_ok = True
    try:
        completed_items = await todoist.get_completed_tasks(_iso_utc(since_dt), now_iso)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (402, 403):
            logger.warning(
                "Todoist completed-tasks endpoint returned %s; skipping recurring pull",
                exc.response.status_code,
            )
            completed_endpoint_ok = False
            completed_items = []
        else:
            raise

    for item in completed_items:
        task_id = item.get("task_id") or item.get("id")
        if not task_id:
            continue
        project_name = todoist.get_project_name(str(item.get("project_id", "")))
        if project_name not in allowed_set:
            continue
        completed_at = item.get("completed_at") or now_iso
        was_inserted = db.record_completion(
            todoist_task_id=str(task_id),
            content=str(item.get("content", "")),
            project_name=project_name,
            labels=[str(lbl) for lbl in item.get("labels", [])],
            completed_at=completed_at,
            source="todoist_sync",
        )
        if was_inserted:
            inserted += 1

    new_token = str(sync_data.get("sync_token") or sync_token)
    db.update_sync_state(new_token, now)

    return {
        "inserted_completions": inserted,
        "sync_token_updated": new_token != sync_token or sync_token == "*",
        "completed_endpoint_ok": completed_endpoint_ok,
    }
