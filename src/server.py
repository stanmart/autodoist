from __future__ import annotations

import hmac
import logging
import re
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

import uvicorn
from fastmcp import FastMCP
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_access_token
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from pydantic import AnyHttpUrl, BaseModel, Field, TypeAdapter, UrlConstraints, field_validator
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from src.config import AppConfig, EnvSettings, load_config, validate_runtime_security
from src.db import Database
from src.sync_worker import run_sync
from src.todoist import CaptureTaskInput, TodoistClient

logger = logging.getLogger(__name__)

_ISSUER_URL_ADAPTER = TypeAdapter(Annotated[AnyHttpUrl, UrlConstraints(preserve_empty_path=True)])
_TASK_ID_RE = re.compile(r"[A-Za-z0-9_-]+")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


class CaptureItem(BaseModel):
    content: str = Field(min_length=1)
    mode: Literal["task", "post_hoc"] = "task"
    project: str
    labels: list[str] = Field(default_factory=list)
    due_string: str | None = None
    priority: int = Field(default=1, ge=1, le=4)
    notes: str | None = None

    @field_validator("content", "project", mode="before")
    @classmethod
    def strip_required_str(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                raise ValueError("value must not be empty")
            return stripped
        return value

    @field_validator("due_string", mode="before")
    @classmethod
    def normalize_due_string(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


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


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _validate_task_id(task_id: str) -> str:
    if not _TASK_ID_RE.fullmatch(task_id):
        raise ValueError(f"Invalid task_id {task_id!r}")
    return task_id


def _validate_labels(labels: list[str], allowed_labels: list[str]) -> list[str]:
    allowed_set = set(allowed_labels)
    invalid = sorted({lbl for lbl in labels if lbl not in allowed_set})
    if invalid:
        raise ValueError(
            f"Unsupported labels {invalid}; allowed labels are: {allowed_labels}"
        )
    return list(dict.fromkeys(labels))


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

    database = db or Database(app_config.storage.db_path)
    database.init_db()
    owns_todoist = todoist is None
    todoist_client = todoist or TodoistClient(env.todoist_api_token)
    rules = app_config.rules
    allowed_projects_set = set(rules.allowed_projects)
    capture_projects_set = set(rules.capture_projects)

    @asynccontextmanager
    async def _lifespan(_server: FastMCP) -> Any:
        try:
            yield
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
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/internal/sync", methods=["POST"])
    async def internal_sync(request: Request) -> JSONResponse:
        expected = env.sync_secret.strip()
        if not expected:
            return JSONResponse({"error": "sync_secret_not_configured"}, status_code=503)
        provided = request.headers.get("X-Sync-Secret", "")
        if not provided or not hmac.compare_digest(
            provided.encode("utf-8"), expected.encode("utf-8")
        ):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        result = await run_sync(database, todoist_client, rules.allowed_projects)
        return JSONResponse({"status": "ok", **result})

    @mcp.tool()
    async def get_tasks_by_synergy(
        project: str | None = None,
        label: str | None = None,
        include_overdue: bool = True,
    ) -> dict[str, Any]:
        """List active tasks grouped by synergy label and sorted by priority and due date."""
        _require_authorized_user(app_config, env)
        if project is not None and project not in allowed_projects_set:
            raise ValueError(
                f"Project {project!r} is not allowed; allowed projects: {rules.allowed_projects}"
            )
        if label is not None:
            _validate_labels([label], rules.allowed_labels)

        project_id = await todoist_client.get_project_id(project) if project else None
        tasks = await todoist_client.get_active_tasks(project_id=project_id, label=label)

        today_str = datetime.now(UTC).strftime("%Y-%m-%d")
        groups: dict[str, list[dict[str, Any]]] = {lbl: [] for lbl in rules.synergy_labels}
        groups["untagged"] = []

        for task in tasks:
            if task.project_name not in allowed_projects_set:
                continue
            if not include_overdue and task.due_date and task.due_date < today_str:
                continue
            bucket = next(
                (lbl for lbl in rules.synergy_labels if lbl in task.labels),
                "untagged",
            )
            groups[bucket].append(
                {
                    "id": task.id,
                    "content": task.content,
                    "project": task.project_name,
                    "labels": task.labels,
                    "priority": task.priority,
                    "due_date": task.due_date,
                    "is_recurring": task.is_recurring,
                }
            )

        for bucket_items in groups.values():
            bucket_items.sort(
                key=lambda t: (-int(t["priority"]), str(t["due_date"] or "9999-99-99"))
            )

        return {k: v for k, v in groups.items() if v}

    @mcp.tool()
    async def capture_items(items: list[CaptureItem]) -> dict[str, Any]:
        """Batch capture tasks (with mandatory review label) or log post-hoc completions."""
        _require_authorized_user(app_config, env)
        if not items:
            return {"created": [], "skipped_duplicates": [], "logged_post_hoc": []}

        # Validate every item before performing any writes
        validated: list[tuple[CaptureItem, list[str]]] = []
        for item in items:
            if item.project not in capture_projects_set:
                raise ValueError(
                    f"Project {item.project!r} is not allowed for capture; "
                    f"allowed capture projects: {rules.capture_projects}"
                )
            clean_labels = _validate_labels(item.labels, rules.allowed_labels)
            validated.append((item, clean_labels))

        task_candidates: list[tuple[CaptureItem, list[str]]] = []
        post_hoc_items: list[tuple[CaptureItem, list[str]]] = []
        for item, clean_labels in validated:
            if item.mode == "post_hoc":
                post_hoc_items.append((item, clean_labels))
            else:
                task_candidates.append((item, clean_labels))

        created: list[dict[str, str]] = []
        skipped_duplicates: list[dict[str, str]] = []

        # Submit Todoist tasks first so transient failures never half-commit post_hoc rows
        if task_candidates:
            existing_by_project: dict[str, set[str]] = {}
            target_projects = sorted({item.project for item, _ in task_candidates})
            for proj_name in target_projects:
                pid = await todoist_client.get_project_id(proj_name)
                active = await todoist_client.get_active_tasks(project_id=pid)
                existing_by_project[proj_name] = {
                    _normalize_text(t.content) for t in active
                }

            batch_inputs: list[CaptureTaskInput] = []
            for item, clean_labels in task_candidates:
                norm = _normalize_text(item.content)
                proj_seen = existing_by_project[item.project]
                if norm in proj_seen:
                    skipped_duplicates.append({"content": item.content, "project": item.project})
                    continue
                proj_seen.add(norm)
                labels_with_review = list(dict.fromkeys([*clean_labels, rules.review_label]))
                batch_inputs.append(
                    CaptureTaskInput(
                        content=item.content,
                        project=item.project,
                        labels=labels_with_review,
                        due_string=item.due_string,
                        priority=item.priority,
                        description=item.notes or "",
                    )
                )

            if batch_inputs:
                created = await todoist_client.batch_add_tasks(batch_inputs)

        logged_post_hoc: list[dict[str, str]] = []
        for item, clean_labels in post_hoc_items:
            database.record_completion(
                todoist_task_id=None,
                content=item.content,
                project_name=item.project,
                labels=clean_labels,
                source="post_hoc_log",
            )
            logged_post_hoc.append({"content": item.content, "project": item.project})

        return {
            "created": created,
            "skipped_duplicates": skipped_duplicates,
            "logged_post_hoc": logged_post_hoc,
        }

    @mcp.tool()
    async def update_or_reschedule(
        task_id: str,
        new_due_date: str | None = None,
        labels: list[str] | None = None,
        priority: int | None = None,
        project: str | None = None,
        clear_review_flag: bool = False,
    ) -> dict[str, Any]:
        """Safely edit a task or move its due date while preserving recurrence."""
        _require_authorized_user(app_config, env)
        _validate_task_id(task_id)
        if new_due_date is not None:
            if not _ISO_DATE_RE.fullmatch(new_due_date):
                raise ValueError("new_due_date must use YYYY-MM-DD format")
            date.fromisoformat(new_due_date)
        if priority is not None and not (1 <= priority <= 4):
            raise ValueError("priority must be between 1 and 4")
        if project is not None and project not in allowed_projects_set:
            raise ValueError(
                f"Project {project!r} is not allowed; allowed projects: {rules.allowed_projects}"
            )
        clean_labels = (
            _validate_labels(labels, rules.allowed_labels)
            if labels is not None
            else None
        )

        await todoist_client.ensure_projects_loaded()
        existing = await todoist_client.get_task(task_id)
        current_project = todoist_client.get_project_name(str(existing.get("project_id") or ""))
        if current_project not in allowed_projects_set:
            raise ValueError(
                f"Task {task_id!r} belongs to project {current_project!r}, "
                "which is outside allowed_projects"
            )

        existing_labels = [str(lbl) for lbl in (existing.get("labels") or [])]
        final_labels: list[str] | None = None
        if clean_labels is not None or clear_review_flag:
            base_labels = list(clean_labels) if clean_labels is not None else list(existing_labels)
            if clear_review_flag:
                base_labels = [lbl for lbl in base_labels if lbl != rules.review_label]
            elif rules.review_label in existing_labels and rules.review_label not in base_labels:
                base_labels.append(rules.review_label)
            final_labels = base_labels

        target_project_id = await todoist_client.get_project_id(project) if project else None
        await todoist_client.update_task(
            task_id,
            existing,
            new_due_date=new_due_date,
            labels=final_labels,
            priority=priority,
            project_id=target_project_id,
        )

        due = existing.get("due")
        preserved_recurrence = bool(
            new_due_date
            and isinstance(due, dict)
            and due.get("is_recurring")
            and due.get("string")
        )
        return {
            "task_id": task_id,
            "updated": True,
            "preserved_recurrence": preserved_recurrence,
        }

    @mcp.tool()
    async def complete_task(task_id: str) -> dict[str, Any]:
        """Mark a task completed in Todoist and immediately log it in completion_log."""
        _require_authorized_user(app_config, env)
        _validate_task_id(task_id)
        await todoist_client.ensure_projects_loaded()
        existing = await todoist_client.get_task(task_id)
        project_name = todoist_client.get_project_name(str(existing.get("project_id") or ""))
        if project_name not in allowed_projects_set:
            raise ValueError(
                f"Task {task_id!r} belongs to project {project_name!r}, "
                "which is outside allowed_projects"
            )

        await todoist_client.close_task(task_id)
        content = str(existing.get("content") or "")
        labels = [str(lbl) for lbl in (existing.get("labels") or [])]
        database.record_completion(
            todoist_task_id=task_id,
            content=content,
            project_name=project_name,
            labels=labels,
            source="mcp_complete",
        )
        return {
            "task_id": task_id,
            "content": content,
            "project": project_name,
            "completed": True,
        }

    @mcp.tool()
    async def get_completion_history(
        days: int = 90,
        project: str | None = None,
    ) -> list[dict[str, str]]:
        """Retrieve compact completion history records for recurrence auditing."""
        _require_authorized_user(app_config, env)
        if project is not None and project not in allowed_projects_set:
            raise ValueError(
                f"Project {project!r} is not allowed; allowed projects: {rules.allowed_projects}"
            )
        return database.get_completion_history(
            days=days,
            project=project,
            allowed_projects=rules.allowed_projects,
        )

    return mcp


def create_app() -> Any:
    app_config, env = load_config()
    mcp = create_server(app_config, env)
    return mcp.http_app(
        transport="streamable-http",
        middleware=build_http_middleware(app_config, env),
    )


def main() -> None:
    uvicorn.run(
        "src.server:create_app",
        factory=True,
        host="0.0.0.0",
        port=8000,
    )


if __name__ == "__main__":
    main()
