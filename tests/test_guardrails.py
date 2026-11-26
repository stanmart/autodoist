from __future__ import annotations

import json
import time
from pathlib import Path

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

from src.config import AppConfig, AuthConfig, EnvSettings, RulesConfig, StorageConfig
from src.db import Database
from src.server import build_http_middleware, create_server
from src.todoist import TODOIST_API_V1_BASE, TodoistClient


def _make_config(
    tmp_path: Path,
    *,
    auth_disabled: bool = True,
    allowed_user_subs: frozenset[str] = frozenset({"sub-allowed"}),
    jwt_audience: str | list[str] | None = "https://mcp.example.com",
    sync_secret: str = "sync-secret-123",
) -> tuple[AppConfig, EnvSettings]:
    cfg = AppConfig(
        storage=StorageConfig(db_path=str(tmp_path / "autodoist.db")),
        auth=AuthConfig(
            mcp_base_url="https://mcp.example.com",
            pocket_id_issuer="https://id.csigahaz.eu",
            jwt_audience=jwt_audience,
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


def _generate_rsa_keypair() -> tuple[rsa.RSAPrivateKey, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_key, public_pem.decode("utf-8")


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

            # Reject ISO week date and path traversal task_id
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

            # Reject task outside allowed_projects
            with pytest.raises(ToolError, match="outside allowed_projects"):
                await client.call_tool(
                    "update_or_reschedule",
                    {"task_id": "t-hidden", "priority": 2},
                )

            # Updating labels with clear_review_flag=False preserves review
            await client.call_tool(
                "update_or_reschedule",
                {"task_id": "t1", "labels": ["home"]},
            )
            # Explicit clear_review_flag=True removes review & preserves recurrence
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

    first_cmd = json.loads(sync_route.calls[0].request.content)["commands"][0]
    assert first_cmd["args"]["labels"] == ["home", "review"]

    second_cmd = json.loads(sync_route.calls[1].request.content)["commands"][0]
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

            res = (await client.call_tool("complete_task", {"task_id": "t42"})).data
            assert res["completed"] is True
            history = (await client.call_tool("get_completion_history", {"days": 7})).data
            assert [h["content"] for h in history] == ["Renew passport"]
    finally:
        await todoist.aclose()

    assert close_route.called


@respx.mock
def test_http_jwt_authz_discovery_and_internal_sync_secret(tmp_path: Path) -> None:
    private_key, public_pem = _generate_rsa_keypair()
    cfg, env = _make_config(
        tmp_path,
        auth_disabled=False,
        allowed_user_subs=frozenset({"sub-allowed"}),
        jwt_audience="https://mcp.example.com",
    )
    verifier = JWTVerifier(
        public_key=public_pem,
        issuer="https://id.csigahaz.eu",
        audience="https://mcp.example.com",
        algorithm="RS256",
    )

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

    mcp = create_server(cfg, env, token_verifier=verifier)
    app = mcp.http_app(
        transport="streamable-http",
        middleware=build_http_middleware(cfg, env),
    )

    now = int(time.time())

    def _mint(sub: str, aud: str = "https://mcp.example.com") -> str:
        return jwt.encode(
            {
                "sub": sub,
                "iss": "https://id.csigahaz.eu",
                "aud": aud,
                "iat": now,
                "exp": now + 300,
            },
            private_key,
            algorithm="RS256",
        )

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

        # RFC 8414 requires exact issuer without spurious trailing slash
        prm = client.get("/.well-known/oauth-protected-resource/mcp")
        assert prm.status_code == 200
        assert prm.json()["authorization_servers"] == ["https://id.csigahaz.eu"]

        # Unauthenticated or wrong audience -> 401
        assert client.post("/mcp").status_code == 401
        wrong_aud = _mint("sub-allowed", aud="other-client")
        assert (
            client.post("/mcp", headers={"Authorization": f"Bearer {wrong_aud}"}).status_code
            == 401
        )

        # Valid JWT for disallowed sub -> 403
        intruder_jwt = _mint("sub-intruder")
        resp_403 = client.post("/mcp", headers={"Authorization": f"Bearer {intruder_jwt}"})
        assert resp_403.status_code == 403
        assert resp_403.json()["error"] == "forbidden"

        # Valid JWT for allowed sub passes auth & authz (reaches MCP handler)
        allowed_jwt = _mint("sub-allowed")
        resp_ok = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {allowed_jwt}",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        assert resp_ok.status_code not in (401, 403)

        # /internal/sync secret checks
        assert client.post("/internal/sync").status_code == 403
        assert (
            client.post("/internal/sync", headers={"X-Sync-Secret": "wrong"}).status_code == 403
        )
        ok_sync = client.post("/internal/sync", headers={"X-Sync-Secret": "sync-secret-123"})
        assert ok_sync.status_code == 200
        assert ok_sync.json()["status"] == "ok"
