from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from autodoist.db import Database
from autodoist.models import CaptureTaskInput, TodoistDue, TodoistProject, TodoistTask
from autodoist.sync_worker import run_sync, sync_loop
from autodoist.todoist import (
    MAX_SYNC_COMMANDS,
    TODOIST_API_V1_BASE,
    AmbiguousProjectError,
    ProjectCache,
    TodoistAPIError,
    TodoistClient,
    shift_due_date,
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_sync_form(req: httpx.Request) -> dict[str, str]:
    assert req.headers["content-type"].startswith("application/x-www-form-urlencoded")
    parsed = parse_qs(req.content.decode("utf-8"), strict_parsing=True)
    return {k: v[0] for k, v in parsed.items()}


def _parse_sync_commands(req: httpx.Request) -> list[dict[str, Any]]:
    form = _parse_sync_form(req)
    commands: list[dict[str, Any]] = json.loads(form["commands"])
    return commands


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
                    cmd["uuid"]: "ok" for cmd in _parse_sync_commands(req)
                },
                "temp_id_mapping": {
                    cmd["temp_id"]: f"real-{idx}"
                    for idx, cmd in enumerate(_parse_sync_commands(req))
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
                    cmd["uuid"]: "ok" for cmd in _parse_sync_commands(req)
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

    commands = _parse_sync_commands(sync_route.calls[0].request)
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
                    _parse_sync_commands(req)[0]["uuid"]: {"error": "Invalid"}
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
    sync_route = respx.post(f"{TODOIST_API_V1_BASE}/sync").mock(
        return_value=httpx.Response(200, json={"sync_token": "tok-2", "items": []})
    )

    client = TodoistClient("test-token", projects_ttl_seconds=0.02)
    try:
        # sync_incremental is pure and does not populate or poison the project cache
        token, items = await client.sync_incremental("tok-1")
        assert token == "tok-2"
        assert items == []
        assert client.get_project_name("p1") is None
        form = _parse_sync_form(sync_route.calls[0].request)
        assert form["sync_token"] == "tok-1"
        assert json.loads(form["resource_types"]) == ["items"]

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
                    cmd["uuid"]: "ok" for cmd in _parse_sync_commands(req)
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
    cmd = _parse_sync_commands(sync_route.calls[0].request)[0]
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


@respx.mock
async def test_run_sync_records_missing_completed_at_once_without_relogging(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "missing_ts.db")
    db.init_db()

    respx.get(f"{TODOIST_API_V1_BASE}/projects").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"id": "p1", "name": "Personal"}], "next_cursor": None},
        )
    )
    respx.post(f"{TODOIST_API_V1_BASE}/sync").mock(
        return_value=httpx.Response(
            200,
            json={
                "sync_token": "tok-skip",
                "items": [
                    {
                        "id": "no-ts-1",
                        "content": "Checked without timestamp",
                        "project_id": "p1",
                        "checked": True,
                        "is_deleted": False,
                        "completed_at": None,
                    }
                ],
            },
        )
    )
    respx.get(f"{TODOIST_API_V1_BASE}/activities").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "object_id": "no-ts-2",
                        "parent_project_id": "p1",
                        "event_date": None,
                        "extra_data": {"content": "Activity without timestamp"},
                    }
                ],
                "next_cursor": None,
            },
        )
    )

    client = TodoistClient("test-token")
    try:
        first = await run_sync(db, client, ["Personal"])
        assert first["inserted_completions"] == 2
        assert {r.content for r in db.get_completion_history(days=30)} == {
            "Checked without timestamp",
            "Activity without timestamp",
        }

        # Reappearing in a later delta with a new sync timestamp does not re-log
        await asyncio.sleep(0.05)
        second = await run_sync(db, client, ["Personal"])
        assert second["inserted_completions"] == 0
        assert len(db.get_completion_history(days=30)) == 2
    finally:
        await client.aclose()


def test_project_cache_never_resolves_a_duplicated_name() -> None:
    cache = ProjectCache()
    cache.replace(
        [
            TodoistProject(id="p1", name="Personal"),
            TodoistProject(id="p2", name="Personal"),
            TodoistProject(id="p3", name="Household"),
            TodoistProject(id="p4", name="Household", is_archived=True),
        ]
    )
    assert cache.is_ambiguous("Personal")
    assert cache.get_id("Personal") is None
    assert cache.get_name("p1") == cache.get_name("p2") == "Personal"
    # An archived namesake does not make the active project ambiguous.
    assert not cache.is_ambiguous("Household")
    assert cache.get_id("Household") == "p3"

    cache.replace([TodoistProject(id="p1", name="Personal")])
    assert not cache.is_ambiguous("Personal")
    assert cache.get_id("Personal") == "p1"


@respx.mock
async def test_get_project_id_raises_for_a_duplicated_name() -> None:
    respx.get(f"{TODOIST_API_V1_BASE}/projects").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": "p1", "name": "Personal"},
                    {"id": "p2", "name": "Personal"},
                ],
                "next_cursor": None,
            },
        )
    )
    client = TodoistClient("test-token")
    try:
        with pytest.raises(AmbiguousProjectError):
            await client.get_project_id("Personal")
        with pytest.raises(ValueError, match="not found"):
            await client.get_project_id("Missing")
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    ("due", "new_date", "expected"),
    [
        (None, "2026-12-05", {"date": "2026-12-05"}),
        (TodoistDue(date="2026-11-30"), "2026-12-05", {"date": "2026-12-05"}),
        # Floating time: keep the wall-clock time as is.
        (
            TodoistDue(date="2026-11-30T09:00:00.000000"),
            "2026-12-05",
            {"date": "2026-12-05T09:00:00.000000"},
        ),
        # Fixed zone, 00:30 in Madrid is 23:30 UTC the day before: the local date moves.
        (
            TodoistDue(date="2026-11-29T23:30:00.000000Z", timezone="Europe/Madrid"),
            "2026-12-05",
            {"date": "2026-12-04T23:30:00.000000Z", "timezone": "Europe/Madrid"},
        ),
        # Fixed zone across a DST change: 09:00 local stays 09:00 local.
        (
            TodoistDue(date="2026-10-20T07:00:00.000000Z", timezone="Europe/Madrid"),
            "2026-10-27",
            {"date": "2026-10-27T08:00:00.000000Z", "timezone": "Europe/Madrid"},
        ),
        # A zone key that is a directory raises OSError inside zoneinfo.
        (
            TodoistDue(date="2026-11-30T09:00:00Z", timezone="America"),
            "2026-12-05",
            {"date": "2026-12-05T09:00:00Z"},
        ),
        # Unknown zone: fall back to swapping the date prefix.
        (
            TodoistDue(date="2026-11-30T09:00:00Z", timezone="Not/AZone"),
            "2026-12-05",
            {"date": "2026-12-05T09:00:00Z"},
        ),
    ],
)
def test_shift_due_date_keeps_the_time_of_day(
    due: TodoistDue | None, new_date: str, expected: dict[str, str]
) -> None:
    assert shift_due_date(due, new_date) == expected


@respx.mock
async def test_update_task_keeps_time_and_recurrence_when_rescheduling() -> None:
    sync_route = respx.post(f"{TODOIST_API_V1_BASE}/sync").mock(
        side_effect=lambda req: httpx.Response(
            200,
            json={"sync_status": {cmd["uuid"]: "ok" for cmd in _parse_sync_commands(req)}},
        )
    )
    client = TodoistClient("test-token")
    existing = TodoistTask(
        id="t1",
        project_id="p1",
        due=TodoistDue(
            date="2026-11-30T09:00:00.000000",
            string="every mon at 9",
            is_recurring=True,
            lang="en",
        ),
    )
    try:
        await client.update_task("t1", existing, new_due_date="2026-12-07")
    finally:
        await client.aclose()

    assert _parse_sync_commands(sync_route.calls[0].request)[0]["args"]["due"] == {
        "date": "2026-12-07T09:00:00.000000",
        "string": "every mon at 9",
        "is_recurring": True,
        "lang": "en",
    }


@respx.mock
async def test_batch_add_tasks_splits_requests_at_the_sync_command_limit() -> None:
    respx.get(f"{TODOIST_API_V1_BASE}/projects").mock(
        return_value=httpx.Response(
            200, json={"results": [{"id": "p1", "name": "Personal"}], "next_cursor": None}
        )
    )
    sync_route = respx.post(f"{TODOIST_API_V1_BASE}/sync").mock(
        side_effect=lambda req: httpx.Response(
            200,
            json={
                "sync_status": {cmd["uuid"]: "ok" for cmd in _parse_sync_commands(req)},
                "temp_id_mapping": {
                    cmd["temp_id"]: f"real-{cmd['args']['content']}"
                    for cmd in _parse_sync_commands(req)
                },
            },
        )
    )
    total = 2 * MAX_SYNC_COMMANDS + 5
    items = [CaptureTaskInput(content=f"task-{i}", project="Personal") for i in range(total)]
    client = TodoistClient("test-token")
    try:
        created = await client.batch_add_tasks(items)
    finally:
        await client.aclose()

    sizes = [len(_parse_sync_commands(call.request)) for call in sync_route.calls]
    assert sizes == [MAX_SYNC_COMMANDS, MAX_SYNC_COMMANDS, 5]
    assert [c.id for c in created] == [f"real-task-{i}" for i in range(total)]


@respx.mock
async def test_run_sync_skips_allowed_names_shared_by_several_projects(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db = Database(tmp_path / "autodoist.db")
    db.init_db()
    respx.get(f"{TODOIST_API_V1_BASE}/projects").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": "p1", "name": "Personal"},
                    {"id": "p2", "name": "Personal"},
                    {"id": "p3", "name": "Household"},
                ],
                "next_cursor": None,
            },
        )
    )
    done_at = _iso(datetime.now(UTC) - timedelta(minutes=5))
    respx.post(f"{TODOIST_API_V1_BASE}/sync").mock(
        return_value=httpx.Response(
            200,
            json={
                "sync_token": "tok-1",
                "items": [
                    {
                        "id": "t-shadow",
                        "content": "Shadow",
                        "project_id": "p2",
                        "checked": True,
                        "completed_at": done_at,
                    },
                    {
                        "id": "t-house",
                        "content": "Mop",
                        "project_id": "p3",
                        "checked": True,
                        "completed_at": done_at,
                    },
                ],
            },
        )
    )
    respx.get(f"{TODOIST_API_V1_BASE}/activities").mock(
        return_value=httpx.Response(200, json={"results": [], "next_cursor": None})
    )

    client = TodoistClient("test-token")
    try:
        with caplog.at_level("WARNING", logger="autodoist.sync_worker"):
            summary = await run_sync(db, client, ["Personal", "Household"])
    finally:
        await client.aclose()

    assert summary["inserted_completions"] == 1
    assert [r.content for r in db.get_completion_history(days=7)] == ["Mop"]
    assert "Personal" in caplog.text
