from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from src.db import Database
from src.sync_worker import run_sync
from src.todoist import TODOIST_API_V1_BASE, CaptureTaskInput, TodoistAPIError, TodoistClient


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
                            "labels": [],
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


@respx.mock
async def test_batch_add_tasks_uses_deterministic_uuids() -> None:
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
        res1 = await client.batch_add_tasks(items, idempotency_key="2026-11-26")
        res2 = await client.batch_add_tasks(items, idempotency_key="2026-11-26")
    finally:
        await client.aclose()

    assert res1 == [{"id": "real-0", "content": "Buy diapers", "project": "Household"}]
    assert res2 == res1

    cmd1 = json.loads(sync_route.calls[0].request.content)["commands"][0]
    cmd2 = json.loads(sync_route.calls[1].request.content)["commands"][0]
    assert cmd1["uuid"] == cmd2["uuid"]
    assert cmd1["temp_id"] == cmd2["temp_id"]


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
                        "completed_at": "2026-11-26T12:00:00Z",
                    },
                    {
                        "id": "102",
                        "content": "Work task",
                        "project_id": "p9",
                        "checked": True,
                        "is_deleted": False,
                        "completed_at": "2026-11-26T12:05:00Z",
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
async def test_run_sync_ingests_recurring_completions_and_dedups(tmp_path: Path) -> None:
    db = Database(tmp_path / "sync_rec.db")
    db.init_db()
    db.update_sync_state("tok-1", "2026-11-26T10:00:00Z")

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
                        "completed_at": "2026-11-26T11:00:00Z",
                    }
                ],
            },
        )
    )
    respx.get(f"{TODOIST_API_V1_BASE}/tasks/completed/by_completion_date").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "task_id": "201",
                        "content": "Empty bins",
                        "project_id": "p2",
                        "completed_at": "2026-11-26T11:00:00Z",
                    },
                    {
                        "task_id": "202",
                        "content": "Water plants",
                        "project_id": "p2",
                        "completed_at": "2026-11-26T11:30:00Z",
                    },
                ],
                "next_cursor": None,
            },
        )
    )

    client = TodoistClient("test-token")
    try:
        summary = await run_sync(db, client, ["Inbox", "Personal", "Household"])
    finally:
        await client.aclose()

    assert summary["inserted_completions"] == 2
    assert summary["completed_endpoint_ok"] is True
    assert db.get_sync_state()[0] == "tok-2"
    assert len(db.get_completion_history(days=90)) == 2
