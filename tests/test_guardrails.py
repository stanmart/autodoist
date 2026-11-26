from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from src.config import AppConfig, AuthConfig, EnvSettings, RulesConfig, StorageConfig
from src.db import Database
from src.server import _AllowedUserASGIMiddleware, build_http_middleware, create_server
from src.todoist import TODOIST_API_V1_BASE, TodoistClient


def _make_config(
    tmp_path: Path,
    *,
    auth_disabled: bool = True,
    allowed_user_subs: frozenset[str] = frozenset({"sub-allowed"}),
    sync_secret: str = "sync-secret-123",
) -> tuple[AppConfig, EnvSettings]:
    cfg = AppConfig(
        storage=StorageConfig(db_path=str(tmp_path / "autodoist.db")),
        auth=AuthConfig(
            mcp_base_url="https://mcp.example.com",
            pocket_id_issuer="https://id.csigahaz.eu",
            allowed_user_subs=allowed_user_subs,
        ),
        rules=RulesConfig(
            allowed_projects=["Inbox", "Personal", "Household"],
            capture_projects=["Personal", "Household"],
            allowed_labels=["errands", "home", "admin", "15min", "review"],
            synergy_labels=["errands", "home", "admin", "15min"],
            review_label="review",
        ),
    )
    env = EnvSettings(
        _env_file=None,
        todoist_api_token="test-token",
        sync_secret=sync_secret,
        auth_disabled=auth_disabled,
    )
    return cfg, env


async def test_only_five_safe_tools_are_registered(tmp_path: Path) -> None:
    cfg, env = _make_config(tmp_path)
    mcp = create_server(cfg, env)
    async with Client(mcp) as client:
        tools = await client.list_tools()
    names = {tool.name for tool in tools}
    assert names == {
        "get_tasks_by_synergy",
        "capture_items",
        "update_or_reschedule",
        "complete_task",
        "get_completion_history",
    }


@respx.mock
async def test_capture_items_guardrails_review_tag_duplicates_and_post_hoc(
    tmp_path: Path,
) -> None:
    respx.get(f"{TODOIST_API_V1_BASE}/projects").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": "p0", "name": "Inbox"},
                    {"id": "p1", "name": "Personal"},
                    {"id": "p2", "name": "Household"},
                ],
                "next_cursor": None,
            },
        )
    )
    respx.get(f"{TODOIST_API_V1_BASE}/tasks").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "existing-1",
                        "content": "  Buy   diapers ",
                        "project_id": "p2",
                        "labels": ["errands"],
                        "priority": 1,
                    }
                ],
                "next_cursor": None,
            },
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
                    cmd["temp_id"]: f"created-{i}"
                    for i, cmd in enumerate(json.loads(req.content)["commands"])
                },
            },
        )
    )

    cfg, env = _make_config(tmp_path)
    db = Database(cfg.storage.db_path)
    todoist = TodoistClient("test-token")
    mcp = create_server(cfg, env, db=db, todoist=todoist)

    try:
        async with Client(mcp) as client:
            # Inbox rejected in capture_items
            with pytest.raises(ToolError, match="not allowed for capture"):
                await client.call_tool(
                    "capture_items",
                    {"items": [{"content": "Triage", "project": "Inbox"}]},
                )

            # Unknown label rejected
            with pytest.raises(ToolError, match="Unsupported labels"):
                await client.call_tool(
                    "capture_items",
                    {
                        "items": [
                            {
                                "content": "Something",
                                "project": "Personal",
                                "labels": ["bogus"],
                            }
                        ]
                    },
                )

            # Valid capture: duplicate skipped, new task gets review label, post_hoc logs to DB
            res = await client.call_tool(
                "capture_items",
                {
                    "items": [
                        {
                            "content": "buy diapers",
                            "mode": "task",
                            "project": "Household",
                            "labels": ["errands"],
                        },
                        {
                            "content": "Fix hinge",
                            "mode": "task",
                            "project": "Household",
                            "labels": ["home"],
                            "priority": 3,
                        },
                        {
                            "content": "Bought oat milk",
                            "mode": "post_hoc",
                            "project": "Household",
                            "labels": ["errands"],
                        },
                    ]
                },
            )
            data = res.data
            assert len(data["created"]) == 1
            assert data["created"][0]["content"] == "Fix hinge"
            assert data["skipped_duplicates"] == [
                {"content": "buy diapers", "project": "Household"}
            ]
            assert data["logged_post_hoc"] == [
                {"content": "Bought oat milk", "project": "Household"}
            ]

            history = (
                await client.call_tool("get_completion_history", {"days": 30})
            ).data
            assert len(history) == 1
            assert history[0]["content"] == "Bought oat milk"
    finally:
        await todoist.aclose()

    sent_cmd = json.loads(sync_route.calls[0].request.content)["commands"][0]
    assert sent_cmd["args"]["labels"] == ["home", "review"]


@respx.mock
async def test_inbox_allowed_in_synergy_and_reschedule_preserves_recurrence(
    tmp_path: Path,
) -> None:
    respx.get(f"{TODOIST_API_V1_BASE}/projects").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": "p0", "name": "Inbox"},
                    {"id": "p1", "name": "Personal"},
                    {"id": "p9", "name": "HiddenWork"},
                ],
                "next_cursor": None,
            },
        )
    )
    respx.get(f"{TODOIST_API_V1_BASE}/tasks").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "t1",
                        "content": "Inbox item",
                        "project_id": "p0",
                        "labels": ["15min"],
                        "priority": 2,
                    },
                    {
                        "id": "t-hidden",
                        "content": "Secret work",
                        "project_id": "p9",
                        "labels": ["15min"],
                        "priority": 4,
                    },
                ],
                "next_cursor": None,
            },
        )
    )
    respx.get(f"{TODOIST_API_V1_BASE}/tasks/t1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "t1",
                "content": "Inbox item",
                "project_id": "p0",
                "labels": ["15min", "review"],
                "due": {
                    "date": "2026-11-26",
                    "string": "every week",
                    "is_recurring": True,
                },
            },
        )
    )
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

    cfg, env = _make_config(tmp_path)
    todoist = TodoistClient("test-token")
    mcp = create_server(cfg, env, todoist=todoist)

    try:
        async with Client(mcp) as client:
            synergy = (await client.call_tool("get_tasks_by_synergy", {})).data
            assert [t["id"] for t in synergy["15min"]] == ["t1"]

            upd = (
                await client.call_tool(
                    "update_or_reschedule",
                    {
                        "task_id": "t1",
                        "new_due_date": "2026-12-01",
                        "clear_review_flag": True,
                    },
                )
            ).data
            assert upd["preserved_recurrence"] is True
    finally:
        await todoist.aclose()

    cmd = json.loads(sync_route.calls[0].request.content)["commands"][0]
    assert cmd["args"]["labels"] == ["15min"]
    assert cmd["args"]["due"]["string"] == "every week"
    assert cmd["args"]["due"]["is_recurring"] is True


@respx.mock
async def test_complete_task_closes_and_logs_completion(tmp_path: Path) -> None:
    respx.get(f"{TODOIST_API_V1_BASE}/projects").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"id": "p1", "name": "Personal"}], "next_cursor": None},
        )
    )
    respx.get(f"{TODOIST_API_V1_BASE}/tasks/t42").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "t42",
                "content": "Renew passport",
                "project_id": "p1",
                "labels": ["admin"],
            },
        )
    )
    close_route = respx.post(f"{TODOIST_API_V1_BASE}/tasks/t42/close").mock(
        return_value=httpx.Response(204)
    )

    cfg, env = _make_config(tmp_path)
    todoist = TodoistClient("test-token")
    mcp = create_server(cfg, env, todoist=todoist)

    try:
        async with Client(mcp) as client:
            res = (await client.call_tool("complete_task", {"task_id": "t42"})).data
            assert res["completed"] is True
            history = (await client.call_tool("get_completion_history", {"days": 7})).data
            assert [h["content"] for h in history] == ["Renew passport"]
    finally:
        await todoist.aclose()

    assert close_route.called


@respx.mock
def test_http_routes_authz_and_internal_sync_secret(tmp_path: Path) -> None:
    cfg, env = _make_config(tmp_path, auth_disabled=False)
    respx.post(f"{TODOIST_API_V1_BASE}/sync").mock(
        return_value=httpx.Response(200, json={"sync_token": "t-1", "items": [], "projects": []})
    )
    respx.get(f"{TODOIST_API_V1_BASE}/projects").mock(
        return_value=httpx.Response(
            200, json={"results": [{"id": "p1", "name": "Personal"}], "next_cursor": None}
        )
    )
    respx.get(f"{TODOIST_API_V1_BASE}/tasks/completed/by_completion_date").mock(
        return_value=httpx.Response(200, json={"items": [], "next_cursor": None})
    )

    mcp = create_server(cfg, env)
    app = mcp.http_app(
        transport="streamable-http",
        middleware=build_http_middleware(cfg, env),
    )

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        prm = client.get("/.well-known/oauth-protected-resource/mcp")
        assert prm.status_code == 200
        assert prm.json()["authorization_servers"] == ["https://id.csigahaz.eu/"]
        assert client.post("/mcp").status_code == 401
        assert client.post("/internal/sync").status_code == 403
        assert (
            client.post("/internal/sync", headers={"X-Sync-Secret": "wrong"}).status_code == 403
        )
        ok_sync = client.post("/internal/sync", headers={"X-Sync-Secret": "sync-secret-123"})
        assert ok_sync.status_code == 200
        assert ok_sync.json()["status"] == "ok"


def test_allowed_user_middleware_returns_403_for_disallowed_sub() -> None:
    async def ok_endpoint(_request: object) -> JSONResponse:
        return JSONResponse({"ok": True})

    base_app = Starlette(routes=[Route("/mcp", ok_endpoint, methods=["POST"])])

    def _with_sub(sub: str) -> _AllowedUserASGIMiddleware:
        mw = _AllowedUserASGIMiddleware(base_app, frozenset({"sub-allowed"}))

        class InjectUser:
            def __init__(self, inner: _AllowedUserASGIMiddleware) -> None:
                self.inner = inner

            async def __call__(self, scope: dict, receive: object, send: object) -> None:
                if scope["type"] == "http":
                    token = AccessToken(
                        token="jwt",
                        client_id="gemini",
                        scopes=[],
                        claims={"sub": sub},
                    )
                    scope["user"] = AuthenticatedUser(token)
                await self.inner(scope, receive, send)

        return InjectUser(mw)  # type: ignore[return-value]

    with TestClient(_with_sub("sub-intruder")) as bad_client:
        resp = bad_client.post("/mcp")
        assert resp.status_code == 403
        assert resp.json()["error"] == "forbidden"

    with TestClient(_with_sub("sub-allowed")) as good_client:
        resp = good_client.post("/mcp")
        assert resp.status_code == 200
