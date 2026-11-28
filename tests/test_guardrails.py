from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.testclient import TestClient

from autodoist.config import (
    AppConfig,
    AuthConfig,
    EnvSettings,
    RulesConfig,
    StorageConfig,
    SyncConfig,
)
from autodoist.db import Database
from autodoist.server import build_http_middleware, create_server
from autodoist.todoist import TODOIST_API_V1_BASE, TodoistClient


def _make_config(
    tmp_path: Path,
    *,
    auth_disabled: bool = True,
    allowed_user_subs: frozenset[str] = frozenset({"sub-allowed"}),
    jwt_audience: str | list[str] | None = "https://mcp.example.com",
    sync_interval_minutes: int = 0,
) -> tuple[AppConfig, EnvSettings]:
    cfg = AppConfig(
        storage=StorageConfig(db_path=str(tmp_path / "autodoist.db")),
        auth=AuthConfig(
            mcp_base_url="https://mcp.example.com",
            oidc_issuer="https://id.example.com",
            jwt_audience=jwt_audience,
            allowed_user_subs=allowed_user_subs,
        ),
        sync=SyncConfig(interval_minutes=sync_interval_minutes),
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
        auth_disabled=auth_disabled,
    )
    return cfg, env


def _generate_rsa_keypair() -> tuple[rsa.RSAPrivateKey, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_key, public_pem.decode("utf-8")


async def _call_json(client: Client, name: str, args: dict[str, Any]) -> dict[str, Any]:
    res = await client.call_tool(name, args)
    assert res.structured_content is not None
    return res.structured_content


def _parse_sync_commands(req: httpx.Request) -> list[dict[str, Any]]:
    assert req.headers["content-type"].startswith("application/x-www-form-urlencoded")
    parsed = parse_qs(req.content.decode("utf-8"), strict_parsing=True)
    commands: list[dict[str, Any]] = json.loads(parsed["commands"][0])
    return commands


async def test_only_the_safe_tools_are_registered(tmp_path: Path) -> None:
    cfg, env = _make_config(tmp_path)
    mcp = create_server(cfg, env)
    async with Client(mcp) as client:
        tools = await client.list_tools()
    names = {tool.name for tool in tools}
    assert names == {
        "get_tasks",
        "capture_items",
        "update_or_reschedule",
        "complete_task",
        "drop_task",
        "get_completion_history",
    }
    # Output schemas are switched off to keep the advertised tool definitions small.
    assert all(tool.output_schema is None for tool in tools)


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
                    cmd["uuid"]: "ok" for cmd in _parse_sync_commands(req)
                },
                "temp_id_mapping": {
                    cmd["temp_id"]: f"created-{i}"
                    for i, cmd in enumerate(_parse_sync_commands(req))
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
            with pytest.raises(ToolError, match="not allowed for capture"):
                await client.call_tool(
                    "capture_items",
                    {"items": [{"content": "Triage", "project": "Inbox"}]},
                )

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

            data = await _call_json(
                client,
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
            assert len(data["created"]) == 1
            assert data["created"][0]["content"] == "Fix hinge"
            assert data["skipped_duplicates"] == [
                {"content": "buy diapers", "project": "Household"}
            ]
            assert data["logged_post_hoc"] == [
                {"content": "Bought oat milk", "project": "Household"}
            ]

            history = (
                await _call_json(client, "get_completion_history", {"days": 30})
            )["completions"]
            assert len(history) == 1
            assert history[0]["content"] == "Bought oat milk"
    finally:
        await todoist.aclose()

    sent_cmd = _parse_sync_commands(sync_route.calls[0].request)[0]
    assert sent_cmd["args"]["labels"] == ["home", "review"]
    # App priority 3 (p3) maps to Todoist API priority 2
    assert sent_cmd["args"]["priority"] == 2


@respx.mock
async def test_capture_items_does_not_commit_post_hoc_when_sync_fails(
    tmp_path: Path,
) -> None:
    respx.get(f"{TODOIST_API_V1_BASE}/projects").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"id": "p1", "name": "Personal"}], "next_cursor": None},
        )
    )
    respx.get(f"{TODOIST_API_V1_BASE}/tasks").mock(
        return_value=httpx.Response(200, json={"results": [], "next_cursor": None})
    )
    respx.post(f"{TODOIST_API_V1_BASE}/sync").mock(
        return_value=httpx.Response(500, json={"error": "upstream down"})
    )

    cfg, env = _make_config(tmp_path)
    db = Database(cfg.storage.db_path)
    todoist = TodoistClient("test-token")
    mcp = create_server(cfg, env, db=db, todoist=todoist)

    try:
        async with Client(mcp) as client:
            with pytest.raises(ToolError):
                await client.call_tool(
                    "capture_items",
                    {
                        "items": [
                            {"content": "New task", "mode": "task", "project": "Personal"},
                            {"content": "Walked dog", "mode": "post_hoc", "project": "Personal"},
                        ]
                    },
                )
            assert db.get_completion_history(days=30) == []
    finally:
        await todoist.aclose()


@respx.mock
async def test_synergy_and_reschedule_guardrails(tmp_path: Path) -> None:
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
    respx.get(f"{TODOIST_API_V1_BASE}/tasks/t-hidden").mock(
        return_value=httpx.Response(
            200,
            json={"id": "t-hidden", "content": "Secret work", "project_id": "p9"},
        )
    )
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

    cfg, env = _make_config(tmp_path)
    todoist = TodoistClient("test-token")
    mcp = create_server(cfg, env, todoist=todoist)

    try:
        async with Client(mcp) as client:
            synergy = (await _call_json(client, "get_tasks", {}))["groups"]
            assert [t["id"] for t in synergy["15min"]] == ["t1"]
            # Todoist API priority 2 maps to app priority 3 (p3)
            assert synergy["15min"][0]["priority"] == 3

            with pytest.raises(ToolError, match="YYYY-MM-DD"):
                await client.call_tool(
                    "update_or_reschedule",
                    {"task_id": "t1", "new_due_date": "2026-W48-1"},
                )
            with pytest.raises(ToolError, match="Invalid task_id"):
                await client.call_tool(
                    "update_or_reschedule",
                    {"task_id": "../secret", "priority": 2},
                )

            with pytest.raises(ToolError, match="outside allowed_projects"):
                await client.call_tool(
                    "update_or_reschedule",
                    {"task_id": "t-hidden", "priority": 2},
                )

            await client.call_tool(
                "update_or_reschedule",
                {"task_id": "t1", "labels": ["home"], "priority": 1},
            )
            upd = await _call_json(
                client,
                "update_or_reschedule",
                {
                    "task_id": "t1",
                    "new_due_date": "2026-12-01",
                    "clear_review_flag": True,
                },
            )
            assert upd["preserved_recurrence"] is True
    finally:
        await todoist.aclose()

    first_cmd = _parse_sync_commands(sync_route.calls[0].request)[0]
    assert first_cmd["args"]["labels"] == ["home", "review"]
    # App priority 1 (p1) maps to Todoist API priority 4
    assert first_cmd["args"]["priority"] == 4

    second_cmd = _parse_sync_commands(sync_route.calls[1].request)[0]
    assert second_cmd["args"]["labels"] == ["15min"]
    assert second_cmd["args"]["due"]["string"] == "every week"
    assert second_cmd["args"]["due"]["is_recurring"] is True


@respx.mock
async def test_complete_task_and_history_project_isolation(tmp_path: Path) -> None:
    respx.get(f"{TODOIST_API_V1_BASE}/projects").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": "p1", "name": "Personal"},
                    {"id": "p9", "name": "HiddenWork"},
                ],
                "next_cursor": None,
            },
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
    respx.get(f"{TODOIST_API_V1_BASE}/tasks/t99").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "t99",
                "content": "Secret work",
                "project_id": "p9",
                "labels": [],
            },
        )
    )
    close_route = respx.post(f"{TODOIST_API_V1_BASE}/tasks/t42/close").mock(
        return_value=httpx.Response(204)
    )

    cfg, env = _make_config(tmp_path)
    db = Database(cfg.storage.db_path)
    db.init_db()
    db.record_completion(
        todoist_task_id="legacy",
        content="Legacy project task",
        project_name="HiddenWork",
        source="todoist_sync",
    )
    todoist = TodoistClient("test-token")
    mcp = create_server(cfg, env, db=db, todoist=todoist)

    try:
        async with Client(mcp) as client:
            with pytest.raises(ToolError, match="outside allowed_projects"):
                await client.call_tool("complete_task", {"task_id": "t99"})

            res = await _call_json(client, "complete_task", {"task_id": "t42"})
            assert res["completed"] is True
            history = (
                await _call_json(client, "get_completion_history", {"days": 7})
            )["completions"]
            assert [h["content"] for h in history] == ["Renew passport"]
    finally:
        await todoist.aclose()

    assert close_route.called


def test_http_jwt_authz_discovery_and_removed_internal_sync(tmp_path: Path) -> None:
    private_key, public_pem = _generate_rsa_keypair()
    cfg, env = _make_config(
        tmp_path,
        auth_disabled=False,
        allowed_user_subs=frozenset({"sub-allowed"}),
        jwt_audience="https://mcp.example.com",
    )
    verifier = JWTVerifier(
        public_key=public_pem,
        issuer="https://id.example.com",
        audience="https://mcp.example.com",
        algorithm="RS256",
    )

    mcp = create_server(cfg, env, token_verifier=verifier)
    app = mcp.http_app(
        transport="streamable-http",
        stateless_http=True,
        json_response=True,
        middleware=build_http_middleware(cfg, env),
    )

    now = int(time.time())

    def _mint(sub: str, aud: str = "https://mcp.example.com") -> str:
        return jwt.encode(
            {
                "sub": sub,
                "iss": "https://id.example.com",
                "aud": aud,
                "iat": now,
                "exp": now + 300,
            },
            private_key,
            algorithm="RS256",
        )

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/icon.png").headers["content-type"] == "image/png"
        assert client.get("/favicon.ico").headers["content-type"] == "image/x-icon"

        prm = client.get("/.well-known/oauth-protected-resource/mcp")
        assert prm.status_code == 200
        assert prm.json()["authorization_servers"] == ["https://id.example.com"]

        assert client.post("/mcp").status_code == 401
        wrong_aud = _mint("sub-allowed", aud="other-client")
        assert (
            client.post("/mcp", headers={"Authorization": f"Bearer {wrong_aud}"}).status_code
            == 401
        )

        intruder_jwt = _mint("sub-intruder")
        resp_403 = client.post("/mcp", headers={"Authorization": f"Bearer {intruder_jwt}"})
        assert resp_403.status_code == 403
        assert resp_403.json()["error"] == "forbidden"

        allowed_jwt = _mint("sub-allowed")
        resp_ok = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {allowed_jwt}",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            },
        )
        assert resp_ok.status_code == 200
        assert resp_ok.headers["content-type"].startswith("application/json")
        server_info = resp_ok.json()["result"]["serverInfo"]
        assert server_info["name"] == "autodoist"
        assert server_info["icons"] == [
            {
                "src": "https://mcp.example.com/icon.png",
                "mimeType": "image/png",
                "sizes": ["128x128"],
            }
        ]

        assert client.post("/internal/sync").status_code == 404

        expired = jwt.encode(
            {
                "sub": "sub-allowed",
                "iss": "https://id.example.com",
                "aud": "https://mcp.example.com",
                "iat": now - 600,
                "exp": now - 60,
            },
            private_key,
            algorithm="RS256",
        )
        assert (
            client.post("/mcp", headers={"Authorization": f"Bearer {expired}"}).status_code == 401
        )
        wrong_iss = jwt.encode(
            {
                "sub": "sub-allowed",
                "iss": "https://evil.example.com",
                "aud": "https://mcp.example.com",
                "iat": now,
                "exp": now + 300,
            },
            private_key,
            algorithm="RS256",
        )
        assert (
            client.post("/mcp", headers={"Authorization": f"Bearer {wrong_iss}"}).status_code
            == 401
        )


@respx.mock
def test_server_lifespan_runs_background_sync_and_cancels_cleanly(tmp_path: Path) -> None:
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
                "sync_token": "tok-bg",
                "items": [],
            },
        )
    )
    respx.get(f"{TODOIST_API_V1_BASE}/activities").mock(
        return_value=httpx.Response(200, json={"results": [], "next_cursor": None})
    )

    cfg, env = _make_config(tmp_path, auth_disabled=True, sync_interval_minutes=30)
    db = Database(cfg.storage.db_path)
    mcp = create_server(cfg, env, db=db)
    app = mcp.http_app(transport="streamable-http")

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        for _ in range(50):
            if db.get_sync_state()[0] == "tok-bg":
                break
            time.sleep(0.01)
    assert db.get_sync_state()[0] == "tok-bg"


async def test_tool_level_authz_blocks_unauthenticated_context(tmp_path: Path) -> None:
    cfg, env = _make_config(tmp_path, auth_disabled=False)
    mcp = create_server(cfg, env)
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="Authentication required"):
            await client.call_tool("get_completion_history", {"days": 7})


@respx.mock
async def test_synergy_sorting_overdue_filtering_and_error_cases(tmp_path: Path) -> None:
    today = datetime.now(UTC).date()
    yesterday = (today - timedelta(days=1)).isoformat()
    tomorrow = (today + timedelta(days=1)).isoformat()
    next_week = (today + timedelta(days=7)).isoformat()

    respx.get(f"{TODOIST_API_V1_BASE}/projects").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"id": "p1", "name": "Personal"}], "next_cursor": None},
        )
    )
    respx.get(f"{TODOIST_API_V1_BASE}/tasks").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "t-overdue",
                        "content": "Overdue",
                        "project_id": "p1",
                        "labels": ["admin"],
                        "priority": 2,
                        "due": {"date": yesterday},
                    },
                    {
                        "id": "t-p4-later",
                        "content": "Low later",
                        "project_id": "p1",
                        "labels": ["admin"],
                        "priority": 1,
                        "due": {"date": next_week},
                    },
                    {
                        "id": "t-p1-soon",
                        "content": "Urgent soon",
                        "project_id": "p1",
                        "labels": ["admin"],
                        "priority": 4,
                        "due": {"date": next_week},
                    },
                    {
                        "id": "t-p1-earlier",
                        "content": "Urgent earlier",
                        "project_id": "p1",
                        "labels": ["admin"],
                        "priority": 4,
                        "due": {"date": tomorrow},
                    },
                    {
                        "id": "t-untagged",
                        "content": "Plain task",
                        "project_id": "p1",
                        "labels": [],
                        "priority": 1,
                    },
                ],
                "next_cursor": None,
            },
        )
    )

    cfg, env = _make_config(tmp_path)
    todoist = TodoistClient("test-token")
    mcp = create_server(cfg, env, todoist=todoist)

    try:
        async with Client(mcp) as client:
            listing = await _call_json(client, "get_tasks", {})
            assert (listing["total"], listing["truncated"]) == (5, False)
            all_tasks = listing["groups"]
            assert [t["id"] for t in all_tasks["admin"]] == [
                "t-p1-earlier",
                "t-p1-soon",
                "t-overdue",
                "t-p4-later",
            ]
            assert [t["priority"] for t in all_tasks["admin"]] == [1, 1, 3, 4]
            assert [t["id"] for t in all_tasks["untagged"]] == ["t-untagged"]
            assert all_tasks["untagged"][0]["priority"] == 4

            async def ids(args: dict[str, Any]) -> list[str]:
                res = await _call_json(client, "get_tasks", {"group_by": "none", **args})
                return [t["id"] for t in res["groups"].get("tasks", [])]

            assert await ids({"due": "overdue"}) == ["t-overdue"]
            assert await ids({"due": "today"}) == ["t-overdue"]
            assert await ids({"due": "week"}) == [
                "t-p1-earlier",
                "t-p1-soon",
                "t-overdue",
                "t-p4-later",
            ]
            assert await ids({"due": "none"}) == ["t-untagged"]

            by_due = (await _call_json(client, "get_tasks", {"group_by": "due"}))["groups"]
            assert {k: [t["id"] for t in v] for k, v in by_due.items()} == {
                "this_week": ["t-p1-earlier", "t-p1-soon", "t-p4-later"],
                "overdue": ["t-overdue"],
                "no_date": ["t-untagged"],
            }

            capped = await _call_json(client, "get_tasks", {"limit": 2, "group_by": "none"})
            assert [t["id"] for t in capped["groups"]["tasks"]] == ["t-p1-earlier", "t-p1-soon"]
            assert (capped["total"], capped["truncated"]) == (5, True)

            with pytest.raises(ToolError, match="not allowed"):
                await client.call_tool("get_tasks", {"project": "WorkSecret"})
            with pytest.raises(ToolError, match="Unsupported labels"):
                await client.call_tool("get_tasks", {"label": "bogus"})
    finally:
        await todoist.aclose()


@respx.mock
async def test_capture_in_batch_dedup_and_mutation_input_guardrails(tmp_path: Path) -> None:
    respx.get(f"{TODOIST_API_V1_BASE}/projects").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"id": "p1", "name": "Personal"}], "next_cursor": None},
        )
    )
    respx.get(f"{TODOIST_API_V1_BASE}/tasks").mock(
        return_value=httpx.Response(200, json={"results": [], "next_cursor": None})
    )
    sync_route = respx.post(f"{TODOIST_API_V1_BASE}/sync").mock(
        side_effect=lambda req: httpx.Response(
            200,
            json={
                "sync_status": {
                    cmd["uuid"]: "ok" for cmd in _parse_sync_commands(req)
                },
                "temp_id_mapping": {
                    cmd["temp_id"]: f"id-{i}"
                    for i, cmd in enumerate(_parse_sync_commands(req))
                },
            },
        )
    )

    cfg, env = _make_config(tmp_path)
    todoist = TodoistClient("test-token")
    mcp = create_server(cfg, env, todoist=todoist)

    try:
        async with Client(mcp) as client:
            res = await _call_json(
                client,
                "capture_items",
                {
                    "items": [
                        {"content": "Call dentist", "project": "Personal"},
                        {"content": "  call   DENTIST ", "project": "Personal"},
                        {"content": "Bought soap", "mode": "post_hoc", "project": "Personal"},
                        {"content": "  bought   SOAP ", "mode": "post_hoc", "project": "Personal"},
                    ]
                },
            )
            assert [c["content"] for c in res["created"]] == ["Call dentist"]
            # Default capture priority is app 4 (p4), which maps to Todoist API priority 1
            assert _parse_sync_commands(sync_route.calls[0].request)[0]["args"]["priority"] == 1
            assert res["logged_post_hoc"] == [{"content": "Bought soap", "project": "Personal"}]
            assert res["skipped_duplicates"] == [
                {"content": "call   DENTIST", "project": "Personal"},
                {"content": "bought   SOAP", "project": "Personal"},
            ]

            # Retrying the same post_hoc item in a follow-up call also skips duplicate
            retry = await _call_json(
                client,
                "capture_items",
                {"items": [{"content": "Bought soap", "mode": "post_hoc", "project": "Personal"}]},
            )
            assert retry["logged_post_hoc"] == []
            assert retry["skipped_duplicates"] == [
                {"content": "Bought soap", "project": "Personal"}
            ]

            with pytest.raises(ToolError, match="not allowed"):
                await client.call_tool(
                    "update_or_reschedule",
                    {"task_id": "t1", "project": "WorkSecret"},
                )
            with pytest.raises(ToolError, match="between 1 and 4"):
                await client.call_tool(
                    "update_or_reschedule",
                    {"task_id": "t1", "priority": 5},
                )
            with pytest.raises(ToolError, match="Invalid task_id"):
                await client.call_tool("complete_task", {"task_id": "../bad"})
    finally:
        await todoist.aclose()


def test_create_server_requires_todoist_token_and_health_checks_db(tmp_path: Path) -> None:
    cfg, env = _make_config(tmp_path)
    no_tok_env = EnvSettings(_env_file=None, todoist_api_token="", auth_disabled=True)
    with pytest.raises(ValueError, match="TODOIST_API_TOKEN"):
        create_server(cfg, no_tok_env)

    class BrokenDB(Database):
        def ping(self) -> None:
            raise RuntimeError("disk I/O error")

    broken_db = BrokenDB(cfg.storage.db_path)
    mcp = create_server(cfg, env, db=broken_db)
    app = mcp.http_app(transport="streamable-http", stateless_http=True, json_response=True)
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 503
        assert resp.json() == {"status": "unhealthy"}


def _mock_projects(projects: list[dict[str, str]]) -> None:
    respx.get(f"{TODOIST_API_V1_BASE}/projects").mock(
        return_value=httpx.Response(200, json={"results": projects, "next_cursor": None})
    )


def _mock_task(task: dict[str, Any]) -> None:
    respx.get(f"{TODOIST_API_V1_BASE}/tasks/{task['id']}").mock(
        return_value=httpx.Response(200, json=task)
    )


def _mock_sync_ok() -> respx.Route:
    return respx.post(f"{TODOIST_API_V1_BASE}/sync").mock(
        side_effect=lambda req: httpx.Response(
            200,
            json={"sync_status": {cmd["uuid"]: "ok" for cmd in _parse_sync_commands(req)}},
        )
    )


@respx.mock
async def test_duplicate_project_names_fail_closed(tmp_path: Path) -> None:
    # Todoist allows two projects named "Personal"; a name allowlist cannot tell them apart.
    _mock_projects(
        [
            {"id": "p1", "name": "Personal"},
            {"id": "p2", "name": "Personal"},
            {"id": "p3", "name": "Household"},
        ]
    )
    _mock_task({"id": "t-shadow", "content": "Shadow", "project_id": "p2", "labels": []})
    _mock_task({"id": "t-real", "content": "Real", "project_id": "p1", "labels": []})
    _mock_task({"id": "t-house", "content": "Mop", "project_id": "p3", "labels": []})
    tasks_route = respx.get(f"{TODOIST_API_V1_BASE}/tasks").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [{"id": "t-house", "content": "Mop", "project_id": "p3"}],
                "next_cursor": None,
            },
        )
    )
    close_shadow = respx.post(f"{TODOIST_API_V1_BASE}/tasks/t-shadow/close").mock(
        return_value=httpx.Response(204)
    )
    close_real = respx.post(f"{TODOIST_API_V1_BASE}/tasks/t-real/close").mock(
        return_value=httpx.Response(204)
    )
    close_house = respx.post(f"{TODOIST_API_V1_BASE}/tasks/t-house/close").mock(
        return_value=httpx.Response(204)
    )
    sync_route = _mock_sync_ok()

    cfg, env = _make_config(tmp_path)
    todoist = TodoistClient("test-token")
    mcp = create_server(cfg, env, todoist=todoist)
    ambiguous = "shared by several Todoist projects"

    try:
        async with Client(mcp) as client:
            for task_id in ("t-shadow", "t-real"):
                with pytest.raises(ToolError, match=ambiguous):
                    await client.call_tool("complete_task", {"task_id": task_id})
                with pytest.raises(ToolError, match=ambiguous):
                    await client.call_tool(
                        "update_or_reschedule", {"task_id": task_id, "priority": 1}
                    )
            with pytest.raises(ToolError, match=ambiguous):
                await client.call_tool("get_tasks", {"project": "Personal"})
            with pytest.raises(ToolError, match=ambiguous):
                await client.call_tool(
                    "capture_items",
                    {"items": [{"content": "Buy stamps", "project": "Personal"}]},
                )
            with pytest.raises(ToolError, match=ambiguous):
                await client.call_tool(
                    "update_or_reschedule", {"task_id": "t-house", "project": "Personal"}
                )

            # Uniquely named projects keep working.
            listed = (await _call_json(client, "get_tasks", {}))["groups"]
            assert [t["id"] for t in listed["untagged"]] == ["t-house"]
            done = await _call_json(client, "complete_task", {"task_id": "t-house"})
            assert done["project"] == "Household"
    finally:
        await todoist.aclose()

    assert not close_shadow.called
    assert not close_real.called
    assert close_house.called
    assert not sync_route.called
    assert tasks_route.called


@respx.mock
async def test_listing_fails_closed_when_a_task_is_in_an_ambiguous_project(
    tmp_path: Path,
) -> None:
    _mock_projects([{"id": "p1", "name": "Personal"}, {"id": "p2", "name": "Personal"}])
    respx.get(f"{TODOIST_API_V1_BASE}/tasks").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [{"id": "t1", "content": "Shadow", "project_id": "p2"}],
                "next_cursor": None,
            },
        )
    )
    cfg, env = _make_config(tmp_path)
    todoist = TodoistClient("test-token")
    mcp = create_server(cfg, env, todoist=todoist)
    try:
        async with Client(mcp) as client:
            with pytest.raises(ToolError, match="shared by several Todoist projects"):
                await client.call_tool("get_tasks", {})
    finally:
        await todoist.aclose()


@respx.mock
async def test_update_labels_keeps_labels_outside_the_allowlist(tmp_path: Path) -> None:
    _mock_projects([{"id": "p1", "name": "Personal"}])
    _mock_task(
        {
            "id": "t1",
            "content": "Call plumber",
            "project_id": "p1",
            "labels": ["waiting", "home", "review", "someday"],
        }
    )
    sync_route = _mock_sync_ok()
    cfg, env = _make_config(tmp_path)
    todoist = TodoistClient("test-token")
    mcp = create_server(cfg, env, todoist=todoist)

    try:
        async with Client(mcp) as client:
            await client.call_tool(
                "update_or_reschedule", {"task_id": "t1", "labels": ["errands"]}
            )
            await client.call_tool(
                "update_or_reschedule",
                {"task_id": "t1", "labels": [], "clear_review_flag": True},
            )
    finally:
        await todoist.aclose()

    # "home" (allowlisted) is replaced; "waiting"/"someday" (hand-set) and review survive.
    first = _parse_sync_commands(sync_route.calls[0].request)[0]["args"]["labels"]
    assert first == ["errands", "waiting", "someday", "review"]
    second = _parse_sync_commands(sync_route.calls[1].request)[0]["args"]["labels"]
    assert second == ["waiting", "someday"]


@respx.mock
async def test_complete_task_does_not_close_or_log_an_already_completed_task(
    tmp_path: Path,
) -> None:
    _mock_projects([{"id": "p1", "name": "Personal"}])
    _mock_task(
        {"id": "t1", "content": "Done before", "project_id": "p1", "checked": True}
    )
    close_route = respx.post(f"{TODOIST_API_V1_BASE}/tasks/t1/close").mock(
        return_value=httpx.Response(204)
    )
    cfg, env = _make_config(tmp_path)
    db = Database(cfg.storage.db_path)
    todoist = TodoistClient("test-token")
    mcp = create_server(cfg, env, db=db, todoist=todoist)

    try:
        async with Client(mcp) as client:
            res = await _call_json(client, "complete_task", {"task_id": "t1"})
    finally:
        await todoist.aclose()

    assert res["completed"] is True
    assert not close_route.called
    assert db.get_completion_history(days=7) == []


def test_auth_disabled_logs_a_startup_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg, env = _make_config(tmp_path, auth_disabled=True)
    with caplog.at_level("WARNING", logger="autodoist.server"):
        create_server(cfg, env, todoist=TodoistClient("test-token"))
    assert "AUTH_DISABLED" in caplog.text


@respx.mock
async def test_renaming_a_duplicate_project_takes_effect_on_the_next_call(
    tmp_path: Path,
) -> None:
    duplicated = [{"id": "p1", "name": "Personal"}, {"id": "p2", "name": "Personal"}]
    renamed = [{"id": "p1", "name": "Personal"}, {"id": "p2", "name": "Archive"}]
    respx.get(f"{TODOIST_API_V1_BASE}/projects").mock(
        side_effect=[
            httpx.Response(200, json={"results": duplicated, "next_cursor": None}),
            httpx.Response(200, json={"results": renamed, "next_cursor": None}),
        ]
    )
    _mock_task({"id": "t1", "content": "Real", "project_id": "p1", "labels": []})
    respx.post(f"{TODOIST_API_V1_BASE}/tasks/t1/close").mock(return_value=httpx.Response(204))
    cfg, env = _make_config(tmp_path)
    todoist = TodoistClient("test-token")
    mcp = create_server(cfg, env, todoist=todoist)
    try:
        async with Client(mcp) as client:
            # Warm the cache with the duplicate; the rename must not wait for the TTL.
            await todoist.refresh_projects()
            assert todoist.is_ambiguous_project_name("Personal") is True
            res = await _call_json(client, "complete_task", {"task_id": "t1"})
            assert res["project"] == "Personal"
    finally:
        await todoist.aclose()


@respx.mock
async def test_update_rewrites_content_notes_and_schedule(tmp_path: Path) -> None:
    _mock_projects([{"id": "p1", "name": "Personal"}])
    _mock_task(
        {
            "id": "t1",
            "content": "diapers??",
            "project_id": "p1",
            "labels": [],
            "due": {"date": "2026-11-20", "string": "every week", "is_recurring": True},
        }
    )
    sync_route = _mock_sync_ok()
    cfg, env = _make_config(tmp_path)
    todoist = TodoistClient("test-token")
    mcp = create_server(cfg, env, todoist=todoist)

    try:
        async with Client(mcp) as client:
            await client.call_tool(
                "update_or_reschedule",
                {
                    "task_id": "t1",
                    "content": "  Buy diapers ",
                    "notes": "Size 4",
                    "due_string": "every 2 weeks on thu",
                },
            )
            await client.call_tool(
                "update_or_reschedule", {"task_id": "t1", "due_string": "No Date"}
            )
            with pytest.raises(ToolError, match="not both"):
                await client.call_tool(
                    "update_or_reschedule",
                    {"task_id": "t1", "new_due_date": "2026-12-01", "due_string": "friday"},
                )
            with pytest.raises(ToolError, match="content must not be empty"):
                await client.call_tool(
                    "update_or_reschedule", {"task_id": "t1", "content": "  "}
                )
    finally:
        await todoist.aclose()

    assert len(sync_route.calls) == 2
    first = _parse_sync_commands(sync_route.calls[0].request)[0]["args"]
    assert first["content"] == "Buy diapers"
    assert first["description"] == "Size 4"
    # A due_string replaces the schedule outright, old recurrence included.
    assert first["due"] == {"string": "every 2 weeks on thu"}
    second = _parse_sync_commands(sync_route.calls[1].request)[0]["args"]
    assert second["due"] is None


@respx.mock
async def test_drop_task_deletes_snapshots_and_stays_out_of_history(tmp_path: Path) -> None:
    _mock_projects([{"id": "p1", "name": "Personal"}, {"id": "p9", "name": "HiddenWork"}])
    _mock_task(
        {
            "id": "t1",
            "content": "Learn the ukulele",
            "description": "Maybe one day",
            "project_id": "p1",
            "labels": ["home"],
        }
    )
    _mock_task({"id": "t2", "content": "Plan trip", "project_id": "p1", "labels": []})
    _mock_task({"id": "t9", "content": "Secret work", "project_id": "p9", "labels": []})
    respx.get(f"{TODOIST_API_V1_BASE}/tasks").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": "t1", "content": "Learn the ukulele", "project_id": "p1"},
                    {"id": "t2", "content": "Plan trip", "project_id": "p1"},
                    {"id": "t3", "content": "Book flights", "project_id": "p1", "parent_id": "t2"},
                ],
                "next_cursor": None,
            },
        )
    )
    delete_route = respx.delete(f"{TODOIST_API_V1_BASE}/tasks/t1").mock(
        return_value=httpx.Response(204)
    )
    cfg, env = _make_config(tmp_path)
    db = Database(cfg.storage.db_path)
    todoist = TodoistClient("test-token")
    mcp = create_server(cfg, env, db=db, todoist=todoist)

    try:
        async with Client(mcp) as client:
            with pytest.raises(ToolError, match="outside allowed_projects"):
                await client.call_tool("drop_task", {"task_id": "t9"})
            with pytest.raises(ToolError, match="has subtasks"):
                await client.call_tool("drop_task", {"task_id": "t2"})

            res = await _call_json(client, "drop_task", {"task_id": "t1"})
            assert res == {
                "task_id": "t1",
                "content": "Learn the ukulele",
                "project": "Personal",
                "dropped": True,
            }
            history = (await _call_json(client, "get_completion_history", {}))["completions"]
            assert history == []
    finally:
        await todoist.aclose()

    assert delete_route.call_count == 1
    with sqlite3.connect(cfg.storage.db_path) as conn:
        rows = conn.execute(
            "SELECT todoist_task_id, content, project_name, task_json FROM dropped_tasks"
        ).fetchall()
    assert [row[:3] for row in rows] == [("t1", "Learn the ukulele", "Personal")]
    snapshot = json.loads(rows[0][3])
    assert snapshot["description"] == "Maybe one day"
    assert snapshot["labels"] == ["home"]


@respx.mock
async def test_get_tasks_slot_triage_someday_and_stale_filters(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    today = now.date()

    def due_in(days: int) -> dict[str, str]:
        return {"date": (today + timedelta(days=days)).isoformat()}

    _mock_projects([{"id": "p0", "name": "Inbox"}, {"id": "p1", "name": "Personal"}])
    tasks: list[dict[str, Any]] = [
        {"id": "t-inbox", "content": "diapers??", "project_id": "p0"},
        {
            "id": "t-review",
            "content": "Call plumber",
            "project_id": "p1",
            "labels": ["review", "weekend"],
        },
        {
            "id": "t-evening",
            "content": "Pay invoice",
            "description": "IBAN is in the mail",
            "project_id": "p1",
            "labels": ["evening"],
            "added_at": (now - timedelta(days=40, hours=1)).isoformat(),
        },
        {
            "id": "t-deadline",
            "content": "Return parcel",
            "project_id": "p1",
            "labels": ["weekend"],
            "due": due_in(1),
        },
        {
            "id": "t-later",
            "content": "Renew passport",
            "project_id": "p1",
            "due": due_in(5),
        },
        {
            "id": "t-someday",
            "content": "Learn the ukulele",
            "project_id": "p1",
            "labels": ["someday"],
        },
    ]

    def _list_tasks(req: httpx.Request) -> httpx.Response:
        # Todoist applies the label filter server-side.
        label = req.url.params.get("label")
        results = [t for t in tasks if label is None or label in t.get("labels", [])]
        return httpx.Response(200, json={"results": results, "next_cursor": None})

    respx.get(f"{TODOIST_API_V1_BASE}/tasks").mock(side_effect=_list_tasks)
    cfg, env = _make_config(tmp_path)
    cfg.rules = RulesConfig.model_validate(
        {
            **cfg.rules.model_dump(),
            "allowed_labels": [*cfg.rules.allowed_labels, "evening", "weekend", "someday"],
            "someday_label": "someday",
            "routines": [{"name": "evening", "due_within_days": 1}, {"name": "weekend"}],
        }
    )
    todoist = TodoistClient("test-token")
    mcp = create_server(cfg, env, todoist=todoist)

    try:
        async with Client(mcp) as client:

            async def ids(args: dict[str, Any]) -> set[str]:
                res = await _call_json(client, "get_tasks", {"group_by": "none", **args})
                return {t["id"] for t in res["groups"].get("tasks", [])}

            # Someday tasks only show up when asked for by label.
            assert "t-someday" not in await ids({})
            assert await ids({"label": "someday"}) == {"t-someday"}

            # A slot is its label plus anything due within its window, whatever the label.
            assert await ids({"slot": "evening"}) == {"t-evening", "t-deadline"}
            assert await ids({"slot": "weekend"}) == {"t-review", "t-deadline"}
            with pytest.raises(ToolError, match="Unknown slot"):
                await client.call_tool("get_tasks", {"slot": "lunch"})

            assert await ids({"needs_triage": True}) == {"t-inbox", "t-review"}
            assert await ids({"older_than_days": 30}) == {"t-evening"}

            by_project = (
                await _call_json(client, "get_tasks", {"group_by": "project"})
            )["groups"]
            assert [t["id"] for t in by_project["Inbox"]] == ["t-inbox"]
            evening = next(t for t in by_project["Personal"] if t["id"] == "t-evening")
            assert evening["notes"] == "IBAN is in the mail"
            assert evening["age_days"] == 40
    finally:
        await todoist.aclose()


def test_routines_and_someday_label_must_use_allowed_labels() -> None:
    base: dict[str, Any] = {
        "allowed_projects": ["Personal"],
        "capture_projects": ["Personal"],
        "allowed_labels": ["review", "evening"],
        "review_label": "review",
    }
    RulesConfig.model_validate({**base, "routines": [{"name": "evening"}]})
    with pytest.raises(ValueError, match="must be present in allowed_labels"):
        RulesConfig.model_validate({**base, "routines": [{"name": "weekend"}]})
    with pytest.raises(ValueError, match="routine names must be unique"):
        RulesConfig.model_validate(
            {**base, "routines": [{"name": "evening"}, {"name": "evening"}]}
        )
    with pytest.raises(ValueError, match="someday_label"):
        RulesConfig.model_validate({**base, "someday_label": "someday"})


def test_server_instructions_describe_the_configured_vocabulary(tmp_path: Path) -> None:
    cfg, env = _make_config(tmp_path)
    cfg.rules = RulesConfig.model_validate(
        {
            **cfg.rules.model_dump(),
            "allowed_labels": [*cfg.rules.allowed_labels, "evening", "someday"],
            "someday_label": "someday",
            "label_descriptions": {"errands": "Needs leaving the house"},
            "project_descriptions": {"Personal": "My own tasks and admin"},
            "routines": [
                {"name": "evening", "purpose": "Urgent tasks.", "capacity": "3 tasks"}
            ],
        }
    )
    instructions = create_server(cfg, env).instructions
    assert instructions is not None
    # It describes itself without claiming every task request, so that other task
    # integrations in the same client keep working.
    assert instructions.startswith("autodoist manages a household's tasks in Todoist.\n")
    assert "Use it for" not in instructions
    assert "Projects (new tasks may be created in: Personal, Household):" in instructions
    assert "- Inbox: raw, hand-captured items that still need triage" in instructions
    assert "- Personal: My own tasks and admin" in instructions
    assert "- Household\n" in instructions
    assert "- errands: Needs leaving the house" in instructions
    assert "- evening: Urgent tasks. Capacity: 3 tasks" in instructions
    # Slot labels are listed once, under the slots.
    assert instructions.count("- evening") == 1
    # Vocabulary only: procedures live in client-side skills.
    assert "Triage" not in instructions

    with pytest.raises(ValueError, match="project_descriptions keys"):
        RulesConfig.model_validate(
            {**cfg.rules.model_dump(), "project_descriptions": {"WorkSecret": "x"}}
        )
    with pytest.raises(ValueError, match="label_descriptions keys"):
        RulesConfig.model_validate(
            {**cfg.rules.model_dump(), "label_descriptions": {"bogus": "x"}}
        )


@respx.mock
async def test_without_a_review_label_nothing_is_tagged_and_triage_is_the_inbox(
    tmp_path: Path,
) -> None:
    _mock_projects([{"id": "p0", "name": "Inbox"}, {"id": "p1", "name": "Personal"}])
    respx.get(f"{TODOIST_API_V1_BASE}/tasks").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": "t-inbox", "content": "diapers??", "project_id": "p0"},
                    {"id": "t-filed", "content": "Pay invoice", "project_id": "p1"},
                ],
                "next_cursor": None,
            },
        )
    )
    sync_route = respx.post(f"{TODOIST_API_V1_BASE}/sync").mock(
        side_effect=lambda req: httpx.Response(
            200,
            json={
                "sync_status": {cmd["uuid"]: "ok" for cmd in _parse_sync_commands(req)},
                "temp_id_mapping": {cmd["temp_id"]: "new-1" for cmd in _parse_sync_commands(req)},
            },
        )
    )
    cfg, env = _make_config(tmp_path)
    cfg.rules = RulesConfig.model_validate(
        {
            **cfg.rules.model_dump(),
            "allowed_labels": ["errands", "home"],
            "synergy_labels": ["errands", "home"],
            "review_label": None,
        }
    )
    todoist = TodoistClient("test-token")
    mcp = create_server(cfg, env, todoist=todoist)

    try:
        async with Client(mcp) as client:
            await client.call_tool(
                "capture_items",
                {
                    "items": [
                        {"content": "Buy stamps", "project": "Personal", "labels": ["errands"]}
                    ]
                },
            )
            triage = await _call_json(
                client, "get_tasks", {"needs_triage": True, "group_by": "none"}
            )
            assert [t["id"] for t in triage["groups"]["tasks"]] == ["t-inbox"]
    finally:
        await todoist.aclose()

    created = _parse_sync_commands(sync_route.calls[0].request)[0]["args"]
    assert created["labels"] == ["errands"]
