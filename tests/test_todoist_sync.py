from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from autodoist.db import Database
from autodoist.models import CaptureTaskInput, TodoistDue, TodoistProject, TodoistTask
from autodoist.sync_worker import run_sync, sync_loop
from autodoist.todoist import TODOIST_API_V1_BASE, ProjectCache, TodoistAPIError, TodoistClient


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@respx.mock
async def test_get_active_tasks_paginates_and_parses_typed_models() -> None:
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
    assert tasks[0].due is not None and tasks[0].due.date == "2026-11-27"
    assert tasks[1].due is None
    assert tasks[1].labels == []


@respx.mock
async def test_batch_add_tasks_creates_and_maps_ids() -> None:
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
        created = await client.batch_add_tasks(items)
    finally:
        await client.aclose()

    assert len(created) == 1
    assert created[0].id == "real-0"
    assert created[0].content == "Buy diapers"
    assert created[0].project == "Household"
    assert sync_route.call_count == 1


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

    existing = TodoistTask(
        id="t99",
        project_id="p1",
        due=TodoistDue(
            date="2026-11-26",
            string="every Monday",
            is_recurring=True,
            lang="en",
        ),
    )

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
async def test_run_sync_ingests_items_and_handles_403_on_activities_endpoint(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    db = Database(tmp_path / "sync.db")
    db.init_db()

    respx.get(f"{TODOIST_API_V1_BASE}/projects").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": "p1", "name": "Personal"},
                    {"id": "p9", "name": "WorkSecret"},
                ],
                "next_cursor": None,
            },
        )
    )
    respx.post(f"{TODOIST_API_V1_BASE}/sync").mock(
        return_value=httpx.Response(
            200,
            json={
                "sync_token": "tok-new",
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
    respx.get(f"{TODOIST_API_V1_BASE}/activities").mock(
        return_value=httpx.Response(403, json={"error": "Pro required"})
    )

    client = TodoistClient("test-token")
    try:
        summary = await run_sync(db, client, ["Inbox", "Personal", "Household"])
    finally:
        await client.aclose()

    assert summary["inserted_completions"] == 1
    assert summary["activities_endpoint_ok"] is False
    token, synced_at = db.get_sync_state()
    assert token == "tok-new"
    assert synced_at is not None

    history = db.get_completion_history(days=90)
    assert len(history) == 1
    assert history[0].content == "Take vitamins"


@respx.mock
async def test_run_sync_paginates_activities_and_preserves_token_on_500(
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
    activities_route = respx.get(f"{TODOIST_API_V1_BASE}/activities").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "object_id": "201",
                            "parent_project_id": "p2",
                            "event_date": _iso(now - timedelta(minutes=30)),
                            "extra_data": {"content": "Empty bins"},
                        }
                    ],
                    "next_cursor": "page-2",
                },
            ),
            httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "object_id": "202",
                            "parent_project_id": "p2",
                            "event_date": _iso(now - timedelta(minutes=15)),
                            "extra_data": {"content": "Water plants", "is_recurring": True},
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
        assert summary["activities_endpoint_ok"] is True
        assert db.get_sync_state()[0] == "tok-2"
        assert len(db.get_completion_history(days=90)) == 2

        with pytest.raises(httpx.HTTPStatusError):
            await run_sync(db, client, ["Inbox", "Personal", "Household"])
        assert db.get_sync_state()[0] == "tok-2"
    finally:
        await client.aclose()

    assert activities_route.call_count == 3


def test_project_cache_replaces_renames_and_archived_projects() -> None:
    cache = ProjectCache(ttl_seconds=60.0)
    assert cache.is_fresh() is False

    cache.replace([TodoistProject(id="p1", name="Personal")])
    assert cache.is_fresh() is True
    assert cache.get_id("Personal") == "p1"
    assert cache.get_name("p1") == "Personal"

    cache.replace([TodoistProject(id="p1", name="PersonalRenamed")])
    assert cache.get_id("Personal") is None
    assert cache.get_name("p1") == "PersonalRenamed"

    cache.replace([TodoistProject(id="p1", name="PersonalRenamed", is_archived=True)])
    assert cache.get_name("p1") is None


@respx.mock
async def test_client_project_cache_ttl_and_pure_sync_incremental() -> None:
    projects_route = respx.get(f"{TODOIST_API_V1_BASE}/projects").mock(
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
        return_value=httpx.Response(200, json={"sync_token": "tok-2", "items": []})
    )

    client = TodoistClient("test-token", projects_ttl_seconds=0.02)
    try:
        # sync_incremental is pure and does not populate or poison the project cache
        token, items = await client.sync_incremental("tok-1")
        assert token == "tok-2"
        assert items == []
        assert client.get_project_name("p1") is None

        assert await client.get_project_id("Personal") == "p1"
        await asyncio.sleep(0.03)
        assert await client.resolve_project_name("p2") == "Household"

        with pytest.raises(ValueError, match="not found in Todoist"):
            await client.get_project_id("DoesNotExist")
    finally:
        await client.aclose()

    assert projects_route.call_count == 3


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

    existing = TodoistTask(id="t50", project_id="p1", due=None)
    client = TodoistClient("test-token")
    try:
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

    respx.get(f"{TODOIST_API_V1_BASE}/projects").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"id": "p1", "name": "Personal"}], "next_cursor": None},
        )
    )
    respx.post(f"{TODOIST_API_V1_BASE}/sync").mock(
        side_effect=[
            httpx.Response(500, json={"error": "temporary"}),
            httpx.Response(
                200,
                json={
                    "sync_token": "tok-loop",
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
    respx.get(f"{TODOIST_API_V1_BASE}/activities").mock(
        return_value=httpx.Response(200, json={"results": [], "next_cursor": None})
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
        assert [r.content for r in db.get_completion_history(days=7)] == ["Loop synced task"]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await client.aclose()
