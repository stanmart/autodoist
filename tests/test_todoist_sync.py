from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from src.db import Database
from src.sync_worker import run_sync, sync_loop
from src.todoist import TODOIST_API_V1_BASE, CaptureTaskInput, TodoistAPIError, TodoistClient


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@respx.mock
async def test_get_active_tasks_paginates_and_strips_metadata() -> None:
    respx.get(f"{TODOIST_API_V1_BASE}/projects").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": "p1", "name": "Personal", "is_deleted": False, "is_archived": False}
                ],
                "next_cursor": None,
            },
        )
    )
    respx.get(f"{TODOIST_API_V1_BASE}/tasks").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "t1",
                            "content": "Pay tax",
                            "project_id": "p1",
                            "labels": ["admin"],
                            "priority": 4,
                            "due": {
                                "date": "2026-11-27",
                                "string": "Nov 27",
                                "is_recurring": False,
                            },
                            "creator_id": "secret-uid",
                        }
                    ],
                    "next_cursor": "cur-2",
                },
            ),
            httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "t2",
                            "content": "Water plants",
                            "project_id": "p1",
                            "labels": None,
                            "priority": 1,
                            "due": None,
                        }
                    ],
                    "next_cursor": None,
                },
            ),
        ]
    )

    client = TodoistClient("test-token")
    try:
        tasks = await client.get_active_tasks()
    finally:
        await client.aclose()

    assert [t.id for t in tasks] == ["t1", "t2"]
    assert tasks[0].project_name == "Personal"
    assert tasks[0].due_date == "2026-11-27"
    assert tasks[1].due_date is None
    assert tasks[1].labels == []


@respx.mock
async def test_batch_add_tasks_idempotency_and_fresh_default_uuids() -> None:
    respx.get(f"{TODOIST_API_V1_BASE}/projects").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"id": "p2", "name": "Household"}], "next_cursor": None},
        )
    )
    sync_route = respx.post(f"{TODOIST_API_V1_BASE}/sync").mock(
        side_effect=lambda req: httpx.Response(
            200,
            json={
                "sync_status": {
                    cmd["uuid"]: "ok" for cmd in json.loads(req.content)["commands"]
                },
                "temp_id_mapping": {
                    cmd["temp_id"]: f"real-{idx}"
                    for idx, cmd in enumerate(json.loads(req.content)["commands"])
                },
            },
        )
    )

    items = [
        CaptureTaskInput(
            content="Buy diapers",
            project="Household",
            labels=["errands", "review"],
            priority=2,
        )
    ]

    client = TodoistClient("test-token")
    try:
        res1 = await client.batch_add_tasks(items, idempotency_key="retry-1")
        res2 = await client.batch_add_tasks(items, idempotency_key="retry-1")
        await client.batch_add_tasks(items)
        await client.batch_add_tasks(items)
    finally:
        await client.aclose()

    assert res1 == [{"id": "real-0", "content": "Buy diapers", "project": "Household"}]
    assert res2 == res1

    cmd_key1 = json.loads(sync_route.calls[0].request.content)["commands"][0]
    cmd_key2 = json.loads(sync_route.calls[1].request.content)["commands"][0]
    assert cmd_key1["uuid"] == cmd_key2["uuid"]
    assert cmd_key1["temp_id"] == cmd_key2["temp_id"]

    # Without explicit idempotency_key, separate calls generate unique UUIDs
    cmd_fresh1 = json.loads(sync_route.calls[2].request.content)["commands"][0]
    cmd_fresh2 = json.loads(sync_route.calls[3].request.content)["commands"][0]
    assert cmd_fresh1["uuid"] != cmd_fresh2["uuid"]


@respx.mock
async def test_update_task_preserves_recurrence_and_moves_project() -> None:
    sync_route = respx.post(f"{TODOIST_API_V1_BASE}/sync").mock(
        side_effect=lambda req: httpx.Response(
            200,
            json={
                "sync_status": {
                    cmd["uuid"]: "ok" for cmd in json.loads(req.content)["commands"]
                }
            },
        )
    )

    existing = {
        "id": "t99",
        "project_id": "p1",
        "due": {
            "date": "2026-11-26",
            "string": "every Monday",
            "is_recurring": True,
            "lang": "en",
        },
    }

    client = TodoistClient("test-token")
    try:
        await client.update_task(
            "t99",
            existing,
            new_due_date="2026-11-30",
            labels=["home"],
            priority=3,
            project_id="p2",
        )
    finally:
        await client.aclose()

    commands = json.loads(sync_route.calls[0].request.content)["commands"]
    assert [c["type"] for c in commands] == ["item_update", "item_move"]
    assert commands[0]["args"]["due"] == {
        "date": "2026-11-30",
        "string": "every Monday",
        "is_recurring": True,
        "lang": "en",
    }
    assert commands[1]["args"] == {"id": "t99", "project_id": "p2"}


@respx.mock
async def test_batch_add_raises_on_sync_command_error() -> None:
    respx.get(f"{TODOIST_API_V1_BASE}/projects").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"id": "p1", "name": "Personal"}], "next_cursor": None},
        )
    )
    respx.post(f"{TODOIST_API_V1_BASE}/sync").mock(
        side_effect=lambda req: httpx.Response(
            200,
            json={
                "sync_status": {
                    json.loads(req.content)["commands"][0]["uuid"]: {"error": "Invalid"}
                }
            },
        )
    )

    client = TodoistClient("test-token")
    try:
        with pytest.raises(TodoistAPIError):
            await client.batch_add_tasks([CaptureTaskInput(content="X", project="Personal")])
    finally:
        await client.aclose()


@respx.mock
async def test_run_sync_ingests_items_and_handles_403_on_completed_endpoint(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    db = Database(tmp_path / "sync.db")
    db.init_db()

    respx.post(f"{TODOIST_API_V1_BASE}/sync").mock(
        return_value=httpx.Response(
            200,
            json={
                "sync_token": "tok-new",
                "projects": [
                    {"id": "p1", "name": "Personal"},
                    {"id": "p9", "name": "WorkSecret"},
                ],
                "items": [
                    {
                        "id": "101",
                        "content": "Take vitamins",
                        "project_id": "p1",
                        "labels": ["home"],
                        "checked": True,
                        "is_deleted": False,
                        "completed_at": _iso(now - timedelta(minutes=10)),
                    },
                    {
                        "id": "102",
                        "content": "Work task",
                        "project_id": "p9",
                        "checked": True,
                        "is_deleted": False,
                        "completed_at": _iso(now - timedelta(minutes=5)),
                    },
                    {
                        "id": "103",
                        "content": "Unchecked",
                        "project_id": "p1",
                        "checked": False,
                        "is_deleted": False,
                    },
                ],
            },
        )
    )
    respx.get(f"{TODOIST_API_V1_BASE}/tasks/completed/by_completion_date").mock(
        return_value=httpx.Response(403, json={"error": "Pro required"})
    )

    client = TodoistClient("test-token")
    try:
        summary = await run_sync(db, client, ["Inbox", "Personal", "Household"])
    finally:
        await client.aclose()

    assert summary["inserted_completions"] == 1
    assert summary["completed_endpoint_ok"] is False
    token, synced_at = db.get_sync_state()
    assert token == "tok-new"
    assert synced_at is not None

    history = db.get_completion_history(days=90)
    assert len(history) == 1
    assert history[0]["content"] == "Take vitamins"


@respx.mock
async def test_run_sync_paginates_completed_tasks_and_preserves_token_on_500(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    db = Database(tmp_path / "sync_rec.db")
    db.init_db()
    db.update_sync_state("tok-1", _iso(now - timedelta(hours=2)))

    respx.get(f"{TODOIST_API_V1_BASE}/projects").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"id": "p2", "name": "Household"}], "next_cursor": None},
        )
    )
    respx.post(f"{TODOIST_API_V1_BASE}/sync").mock(
        return_value=httpx.Response(
            200,
            json={
                "sync_token": "tok-2",
                "items": [
                    {
                        "id": "201",
                        "content": "Empty bins",
                        "project_id": "p2",
                        "checked": True,
                        "is_deleted": False,
                        "completed_at": _iso(now - timedelta(minutes=30)),
                    }
                ],
            },
        )
    )
    completed_route = respx.get(
        f"{TODOIST_API_V1_BASE}/tasks/completed/by_completion_date"
    ).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "task_id": "201",
                            "content": "Empty bins",
                            "project_id": "p2",
                            "completed_at": _iso(now - timedelta(minutes=30)),
                        }
                    ],
                    "next_cursor": "page-2",
                },
            ),
            httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "task_id": "202",
                            "content": "Water plants",
                            "project_id": "p2",
                            "completed_at": _iso(now - timedelta(minutes=15)),
                        }
                    ],
                    "next_cursor": None,
                },
            ),
            httpx.Response(500, json={"error": "Server error"}),
        ]
    )

    client = TodoistClient("test-token")
    try:
        summary = await run_sync(db, client, ["Inbox", "Personal", "Household"])
        assert summary["inserted_completions"] == 2
        assert summary["completed_endpoint_ok"] is True
        assert db.get_sync_state()[0] == "tok-2"
        assert len(db.get_completion_history(days=90)) == 2

        # On 500 error, sync_token must NOT advance
        with pytest.raises(httpx.HTTPStatusError):
            await run_sync(db, client, ["Inbox", "Personal", "Household"])
        assert db.get_sync_state()[0] == "tok-2"
    finally:
        await client.aclose()

    assert completed_route.call_count == 3


@respx.mock
async def test_project_cache_eviction_rename_and_mid_lifetime_refresh(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    db = Database(tmp_path / "cache.db")
    db.init_db()
    db.update_sync_state("tok-1", _iso(now - timedelta(hours=1)))

    respx.get(f"{TODOIST_API_V1_BASE}/projects").mock(
        side_effect=[
            httpx.Response(
                200,
                json={"results": [{"id": "p1", "name": "Personal"}], "next_cursor": None},
            ),
            httpx.Response(
                200,
                json={
                    "results": [
                        {"id": "p1", "name": "Personal"},
                        {"id": "p2", "name": "Household"},
                    ],
                    "next_cursor": None,
                },
            ),
            httpx.Response(
                200,
                json={
                    "results": [
                        {"id": "p1", "name": "Personal"},
                        {"id": "p2", "name": "Household"},
                    ],
                    "next_cursor": None,
                },
            ),
        ]
    )
    respx.post(f"{TODOIST_API_V1_BASE}/sync").mock(
        return_value=httpx.Response(
            200,
            json={
                "sync_token": "tok-2",
                "items": [
                    {
                        "id": "301",
                        "content": "Mop floor",
                        "project_id": "p2",
                        "checked": True,
                        "is_deleted": False,
                        "completed_at": _iso(now - timedelta(minutes=5)),
                    }
                ],
            },
        )
    )
    respx.get(f"{TODOIST_API_V1_BASE}/tasks/completed/by_completion_date").mock(
        return_value=httpx.Response(200, json={"items": [], "next_cursor": None})
    )

    client = TodoistClient("test-token")
    try:
        # Warm cache with only p1, then test rename/archive eviction directly
        assert await client.get_project_id("Personal") == "p1"
        client.update_projects_cache([{"id": "p1", "name": "PersonalRenamed"}])
        assert client.get_project_name("p1") == "PersonalRenamed"
        assert "Personal" not in client._name_to_id
        client.update_projects_cache(
            [{"id": "p1", "name": "PersonalRenamed", "is_archived": True}]
        )
        assert client.get_project_name("p1") is None

        # Restore p1 without p2 so run_sync sees unknown p2 and forces refresh
        client.update_projects_cache([{"id": "p1", "name": "Personal"}])
        summary = await run_sync(db, client, ["Inbox", "Personal", "Household"])
        assert summary["inserted_completions"] == 1
        assert db.get_completion_history(days=7)[0]["content"] == "Mop floor"

        with pytest.raises(ValueError, match="not found in Todoist"):
            await client.get_project_id("DoesNotExist")
    finally:
        await client.aclose()


@respx.mock
async def test_update_task_non_recurring_and_noop() -> None:
    sync_route = respx.post(f"{TODOIST_API_V1_BASE}/sync").mock(
        side_effect=lambda req: httpx.Response(
            200,
            json={
                "sync_status": {
                    cmd["uuid"]: "ok" for cmd in json.loads(req.content)["commands"]
                }
            },
        )
    )

    existing = {"id": "t50", "project_id": "p1", "due": None}
    client = TodoistClient("test-token")
    try:
        # No-op makes zero HTTP requests
        await client.update_task("t50", existing, project_id="p1")
        assert sync_route.call_count == 0

        await client.update_task("t50", existing, new_due_date="2026-12-05")
    finally:
        await client.aclose()

    assert sync_route.call_count == 1
    cmd = json.loads(sync_route.calls[0].request.content)["commands"][0]
    assert cmd["type"] == "item_update"
    assert cmd["args"]["due"] == {"date": "2026-12-05"}


@respx.mock
async def test_sync_loop_recovers_from_transient_error_and_cancels_cleanly(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    db = Database(tmp_path / "loop.db")
    db.init_db()

    respx.post(f"{TODOIST_API_V1_BASE}/sync").mock(
        side_effect=[
            httpx.Response(500, json={"error": "temporary"}),
            httpx.Response(
                200,
                json={
                    "sync_token": "tok-loop",
                    "projects": [{"id": "p1", "name": "Personal"}],
                    "items": [
                        {
                            "id": "501",
                            "content": "Loop synced task",
                            "project_id": "p1",
                            "checked": True,
                            "is_deleted": False,
                            "completed_at": _iso(now - timedelta(minutes=1)),
                        }
                    ],
                },
            ),
        ]
    )
    respx.get(f"{TODOIST_API_V1_BASE}/tasks/completed/by_completion_date").mock(
        return_value=httpx.Response(200, json={"items": [], "next_cursor": None})
    )

    client = TodoistClient("test-token")
    task = asyncio.create_task(
        sync_loop(db, client, ["Personal"], interval_seconds=0.02)
    )
    try:
        for _ in range(50):
            if db.get_sync_state()[0] == "tok-loop":
                break
            await asyncio.sleep(0.01)
        assert db.get_sync_state()[0] == "tok-loop"
        assert [r["content"] for r in db.get_completion_history(days=7)] == ["Loop synced task"]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await client.aclose()
