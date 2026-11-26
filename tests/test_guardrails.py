from __future__ import annotations

import json
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
            )["result"]
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
            synergy = await _call_json(client, "get_tasks_by_synergy", {})
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
            )["result"]
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
        assert resp_ok.json()["result"]["serverInfo"]["name"] == "autodoist"

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
            all_tasks = await _call_json(client, "get_tasks_by_synergy", {})
            assert [t["id"] for t in all_tasks["admin"]] == [
                "t-p1-earlier",
                "t-p1-soon",
                "t-overdue",
                "t-p4-later",
            ]
            assert [t["priority"] for t in all_tasks["admin"]] == [1, 1, 3, 4]
            assert [t["id"] for t in all_tasks["untagged"]] == ["t-untagged"]
            assert all_tasks["untagged"][0]["priority"] == 4

            no_overdue = await _call_json(
                client, "get_tasks_by_synergy", {"include_overdue": False}
            )
            assert "t-overdue" not in [t["id"] for t in no_overdue["admin"]]

            with pytest.raises(ToolError, match="not allowed"):
                await client.call_tool("get_tasks_by_synergy", {"project": "WorkSecret"})
            with pytest.raises(ToolError, match="Unsupported labels"):
                await client.call_tool("get_tasks_by_synergy", {"label": "bogus"})
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
