from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from typing import Any

import uvicorn
from fastmcp import FastMCP
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_access_token
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from pydantic import AnyHttpUrl, TypeAdapter, UrlConstraints
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from autodoist.config import (
    AppConfig,
    EnvSettings,
    load_config,
    validate_runtime_security,
    validate_todoist_token,
)
from autodoist.db import Database
from autodoist.models import (
    CaptureItem,
    CaptureResult,
    CompleteTaskResult,
    CompletionHistoryItem,
    TaskOut,
    UpdateTaskResult,
)
from autodoist.service import AutodoistService
from autodoist.sync_worker import sync_loop
from autodoist.todoist import TodoistClient

logger = logging.getLogger(__name__)

_ISSUER_URL_ADAPTER = TypeAdapter(AnyHttpUrl)
try:
    from typing import Annotated

    _ISSUER_URL_ADAPTER = TypeAdapter(
        Annotated[AnyHttpUrl, UrlConstraints(preserve_empty_path=True)]
    )
except Exception:  # pragma: no cover
    pass


class _AllowedUserASGIMiddleware:
    """Reject authenticated JWTs whose `sub` is not in the configured allowlist with 403."""

    def __init__(self, app: ASGIApp, allowed_subs: frozenset[str]) -> None:
        self.app = app
        self.allowed_subs = allowed_subs

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            user = scope.get("user")
            if isinstance(user, AuthenticatedUser):
                claims = getattr(user.access_token, "claims", None)
                sub = claims.get("sub") if isinstance(claims, dict) else None
                if not isinstance(sub, str) or sub not in self.allowed_subs:
                    resp = JSONResponse(
                        {"error": "forbidden", "error_description": "User not in allowlist"},
                        status_code=403,
                    )
                    await resp(scope, receive, send)
                    return
        await self.app(scope, receive, send)


def _require_authorized_user(app_config: AppConfig, env: EnvSettings) -> None:
    if env.auth_disabled:
        return
    token = get_access_token()
    if token is None:
        raise PermissionError("Authentication required")
    sub = token.claims.get("sub")
    if not isinstance(sub, str) or sub not in app_config.auth.allowed_user_subs:
        raise PermissionError("User is not authorized to access this server")


def build_auth_provider(
    app_config: AppConfig,
    env: EnvSettings,
    *,
    token_verifier: JWTVerifier | None = None,
) -> RemoteAuthProvider | None:
    if env.auth_disabled:
        return None
    issuer = app_config.auth.oidc_issuer
    jwks_uri = app_config.auth.oidc_jwks_uri or f"{issuer}/.well-known/jwks.json"
    if app_config.auth.jwt_audience is None and token_verifier is None:
        logger.warning(
            "auth.jwt_audience is unset; set it once your client/OIDC provider aud is verified"
        )
    verifier = token_verifier or JWTVerifier(
        jwks_uri=jwks_uri,
        issuer=issuer,
        audience=app_config.auth.jwt_audience,
        algorithm=app_config.auth.jwt_algorithm,
    )
    return RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[_ISSUER_URL_ADAPTER.validate_python(issuer)],
        base_url=AnyHttpUrl(app_config.auth.mcp_base_url),
        resource_name="autodoist",
    )


def build_http_middleware(app_config: AppConfig, env: EnvSettings) -> list[Middleware]:
    if env.auth_disabled:
        return []
    return [
        Middleware(
            _AllowedUserASGIMiddleware,
            allowed_subs=app_config.auth.allowed_user_subs,
        )
    ]


def create_server(
    app_config: AppConfig,
    env: EnvSettings,
    *,
    db: Database | None = None,
    todoist: TodoistClient | None = None,
    token_verifier: JWTVerifier | None = None,
) -> FastMCP:
    validate_runtime_security(app_config, env)
    if todoist is None:
        validate_todoist_token(env)

    database = db or Database(app_config.storage.db_path)
    database.init_db()
    owns_todoist = todoist is None
    todoist_client = todoist or TodoistClient(env.todoist_api_token)
    service = AutodoistService(app_config.rules, database, todoist_client)

    @asynccontextmanager
    async def _lifespan(_server: FastMCP) -> Any:
        sync_task: asyncio.Task[None] | None = None
        if app_config.sync.interval_minutes > 0 and (
            todoist is not None or bool(env.todoist_api_token.strip())
        ):
            sync_task = asyncio.create_task(
                sync_loop(
                    database,
                    todoist_client,
                    app_config.rules.allowed_projects,
                    app_config.sync.interval_minutes * 60,
                )
            )
        try:
            yield
        finally:
            try:
                if sync_task is not None:
                    sync_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await sync_task
            finally:
                if owns_todoist:
                    await todoist_client.aclose()

    mcp = FastMCP(
        name="autodoist",
        auth=build_auth_provider(app_config, env, token_verifier=token_verifier),
        lifespan=_lifespan,
    )

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        try:
            await asyncio.to_thread(database.ping)
        except Exception:
            logger.exception("Health check failed")
            return JSONResponse({"status": "unhealthy"}, status_code=503)
        return JSONResponse({"status": "ok"})

    @mcp.tool()
    async def get_tasks_by_synergy(
        project: str | None = None,
        label: str | None = None,
        include_overdue: bool = True,
    ) -> dict[str, list[TaskOut]]:
        """List active tasks grouped by synergy label and sorted by priority and due date."""
        _require_authorized_user(app_config, env)
        return await service.get_tasks_by_synergy(
            project=project,
            label=label,
            include_overdue=include_overdue,
        )

    @mcp.tool()
    async def capture_items(items: list[CaptureItem]) -> CaptureResult:
        """Batch capture tasks (with mandatory review label) or log post-hoc completions."""
        _require_authorized_user(app_config, env)
        return await service.capture_items(items)

    @mcp.tool()
    async def update_or_reschedule(
        task_id: str,
        new_due_date: str | None = None,
        labels: list[str] | None = None,
        priority: int | None = None,
        project: str | None = None,
        clear_review_flag: bool = False,
    ) -> UpdateTaskResult:
        """Safely edit a task or move its due date while preserving recurrence."""
        _require_authorized_user(app_config, env)
        return await service.update_or_reschedule(
            task_id=task_id,
            new_due_date=new_due_date,
            labels=labels,
            priority=priority,
            project=project,
            clear_review_flag=clear_review_flag,
        )

    @mcp.tool()
    async def complete_task(task_id: str) -> CompleteTaskResult:
        """Mark a task completed in Todoist and immediately log it in completion_log."""
        _require_authorized_user(app_config, env)
        return await service.complete_task(task_id)

    @mcp.tool()
    async def get_completion_history(
        days: int = 90,
        project: str | None = None,
    ) -> list[CompletionHistoryItem]:
        """Retrieve compact completion history records for recurrence auditing."""
        _require_authorized_user(app_config, env)
        return await service.get_completion_history(days=days, project=project)

    return mcp


def create_app() -> Any:
    app_config, env = load_config()
    mcp = create_server(app_config, env)
    return mcp.http_app(
        transport="streamable-http",
        stateless_http=True,
        json_response=True,
        middleware=build_http_middleware(app_config, env),
    )


def main() -> None:
    uvicorn.run(
        "autodoist.server:create_app",
        factory=True,
        host="0.0.0.0",
        port=8000,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
