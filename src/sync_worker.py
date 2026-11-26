from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from src.config import load_config
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


async def _resolve_project_name(
    todoist: TodoistClient,
    project_id: str,
    refreshed: list[bool],
) -> str | None:
    name = todoist.get_project_name(project_id)
    if name is None and not refreshed[0]:
        await todoist.ensure_projects_loaded(force_refresh=True)
        refreshed[0] = True
        name = todoist.get_project_name(project_id)
    return name


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
    refreshed_projects = [sync_token == "*"]

    for item in sync_data.get("items") or []:
        if item.get("is_deleted") or not item.get("checked"):
            continue
        project_id = str(item.get("project_id") or "")
        project_name = await _resolve_project_name(todoist, project_id, refreshed_projects)
        if project_name not in allowed_set:
            continue
        completed_at = item.get("completed_at") or now_iso
        was_inserted = db.record_completion(
            todoist_task_id=str(item["id"]),
            content=str(item.get("content") or ""),
            project_name=project_name,
            labels=[str(lbl) for lbl in (item.get("labels") or [])],
            completed_at=completed_at,
            source="todoist_sync",
        )
        if was_inserted:
            inserted += 1

    # Best-effort completed-tasks pull (captures recurring completions when supported by plan)
    lookback_limit = now - timedelta(days=90)
    if last_synced_at:
        since_dt = max(_parse_iso_utc(last_synced_at) - timedelta(hours=1), lookback_limit)
    else:
        since_dt = now - timedelta(days=7)

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
        project_id = str(item.get("project_id") or "")
        project_name = await _resolve_project_name(todoist, project_id, refreshed_projects)
        if project_name not in allowed_set:
            continue
        completed_at = item.get("completed_at") or now_iso
        was_inserted = db.record_completion(
            todoist_task_id=str(task_id),
            content=str(item.get("content") or ""),
            project_name=project_name,
            labels=[str(lbl) for lbl in (item.get("labels") or [])],
            completed_at=completed_at,
            source="todoist_sync",
        )
        if was_inserted:
            inserted += 1

    raw_new_token = sync_data.get("sync_token")
    new_token = str(raw_new_token) if raw_new_token else sync_token
    db.update_sync_state(new_token, now)

    return {
        "inserted_completions": inserted,
        "sync_token_updated": bool(raw_new_token) and str(raw_new_token) != sync_token,
        "completed_endpoint_ok": completed_endpoint_ok,
    }


async def sync_loop(
    db: Database,
    todoist: TodoistClient,
    allowed_projects: Iterable[str],
    interval_seconds: float,
) -> None:
    """Run incremental sync immediately and then periodically until cancelled."""
    while True:
        try:
            summary = await run_sync(db, todoist, allowed_projects)
            logger.info("Periodic sync completed: %s", summary)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Periodic sync failed; will retry on next interval")
        await asyncio.sleep(interval_seconds)


async def _run_cli() -> None:
    app_config, env = load_config()
    if not env.todoist_api_token.strip():
        raise ValueError("TODOIST_API_TOKEN must be set")
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
