from __future__ import annotations

import json
import time
import uuid
from collections.abc import Sequence
from typing import Any

import httpx

from autodoist.models import (
    CaptureTaskInput,
    CreatedTaskOut,
    TodoistActivityEvent,
    TodoistProject,
    TodoistTask,
)

TODOIST_API_V1_BASE = "https://api.todoist.com/api/v1"


class TodoistAPIError(RuntimeError):
    """Raised when Todoist returns an application-level /sync command error."""


class ProjectCache:
    """TTL-backed bi-directional mapping between active Todoist project IDs and names."""

    def __init__(self, ttl_seconds: float = 600.0) -> None:
        self._ttl_seconds = ttl_seconds
        self._loaded_at: float | None = None
        self._by_id: dict[str, str] = {}
        self._by_name: dict[str, str] = {}

    def is_fresh(self) -> bool:
        if self._loaded_at is None:
            return False
        return (time.monotonic() - self._loaded_at) < self._ttl_seconds

    def replace(self, projects: Sequence[TodoistProject]) -> None:
        by_id: dict[str, str] = {}
        by_name: dict[str, str] = {}
        for proj in projects:
            if proj.is_deleted or proj.is_archived:
                continue
            by_id[proj.id] = proj.name
            by_name[proj.name] = proj.id
        self._by_id = by_id
        self._by_name = by_name
        self._loaded_at = time.monotonic()

    def get_id(self, name: str) -> str | None:
        return self._by_name.get(name)

    def get_name(self, project_id: str) -> str | None:
        return self._by_id.get(project_id)


class TodoistClient:
    """Async client for Todoist API v1 (REST resources + /sync commands)."""

    def __init__(
        self,
        api_token: str,
        *,
        base_url: str = TODOIST_API_V1_BASE,
        client: httpx.AsyncClient | None = None,
        projects_ttl_seconds: float = 600.0,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=20.0,
        )
        self._projects = ProjectCache(ttl_seconds=projects_ttl_seconds)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def refresh_projects(self, *, force: bool = False) -> bool:
        if not force and self._projects.is_fresh():
            return False
        projects: list[TodoistProject] = []
        cursor: str | None = None
        while True:
            params: dict[str, str] = {}
            if cursor:
                params["cursor"] = cursor
            resp = await self._client.get("/projects", params=params)
            resp.raise_for_status()
            payload = resp.json()
            for raw in payload.get("results") or []:
                projects.append(TodoistProject.model_validate(raw))
            cursor = payload.get("next_cursor")
            if not cursor:
                break
        self._projects.replace(projects)
        return True

    async def get_project_id(self, name: str) -> str:
        refreshed = await self.refresh_projects()
        pid = self._projects.get_id(name)
        if pid is None and not refreshed:
            await self.refresh_projects(force=True)
            pid = self._projects.get_id(name)
        if pid is None:
            raise ValueError(f"Project {name!r} not found in Todoist")
        return pid

    def get_project_name(self, project_id: str) -> str | None:
        return self._projects.get_name(project_id)

    async def resolve_project_name(self, project_id: str) -> str | None:
        if not project_id:
            return None
        refreshed = await self.refresh_projects()
        name = self._projects.get_name(project_id)
        if name is None and not refreshed:
            await self.refresh_projects(force=True)
            name = self._projects.get_name(project_id)
        return name

    async def get_active_tasks(
        self,
        *,
        project_id: str | None = None,
        label: str | None = None,
    ) -> list[TodoistTask]:
        await self.refresh_projects()
        tasks: list[TodoistTask] = []
        cursor: str | None = None
        while True:
            params: dict[str, str] = {}
            if project_id:
                params["project_id"] = project_id
            if label:
                params["label"] = label
            if cursor:
                params["cursor"] = cursor
            resp = await self._client.get("/tasks", params=params)
            resp.raise_for_status()
            payload = resp.json()
            for raw in payload.get("results") or []:
                task = TodoistTask.model_validate(raw)
                task.project_name = self._projects.get_name(task.project_id)
                tasks.append(task)
            cursor = payload.get("next_cursor")
            if not cursor:
                break
        return tasks

    async def get_task(self, task_id: str) -> TodoistTask:
        resp = await self._client.get(f"/tasks/{task_id}")
        resp.raise_for_status()
        return TodoistTask.model_validate(resp.json())

    async def batch_add_tasks(self, items: list[CaptureTaskInput]) -> list[CreatedTaskOut]:
        if not items:
            return []
        await self.refresh_projects()

        commands: list[dict[str, Any]] = []
        temp_meta: list[tuple[str, CaptureTaskInput]] = []
        for item in items:
            project_id = await self.get_project_id(item.project)
            args: dict[str, Any] = {
                "content": item.content,
                "project_id": project_id,
                "labels": list(item.labels),
                "priority": item.priority,
            }
            if item.due_string:
                args["due"] = {"string": item.due_string}
            if item.description:
                args["description"] = item.description

            temp_id = str(uuid.uuid4())
            commands.append(
                {
                    "type": "item_add",
                    "temp_id": temp_id,
                    "uuid": str(uuid.uuid4()),
                    "args": args,
                }
            )
            temp_meta.append((temp_id, item))

        data = await self._post_sync(commands=commands)
        self._raise_on_sync_errors(data)

        mapping = data.get("temp_id_mapping", {})
        return [
            CreatedTaskOut(
                id=str(mapping.get(temp_id, "")),
                content=item.content,
                project=item.project,
            )
            for temp_id, item in temp_meta
        ]

    async def update_task(
        self,
        task_id: str,
        existing_task: TodoistTask,
        *,
        new_due_date: str | None = None,
        labels: list[str] | None = None,
        priority: int | None = None,
        project_id: str | None = None,
    ) -> None:
        commands: list[dict[str, Any]] = []
        update_args: dict[str, Any] = {"id": task_id}

        if new_due_date is not None:
            due = existing_task.due
            if due is not None and due.is_recurring and due.string:
                due_payload: dict[str, Any] = {
                    "date": new_due_date,
                    "string": due.string,
                    "is_recurring": True,
                }
                if due.lang:
                    due_payload["lang"] = due.lang
                update_args["due"] = due_payload
            else:
                update_args["due"] = {"date": new_due_date}

        if labels is not None:
            update_args["labels"] = list(labels)
        if priority is not None:
            update_args["priority"] = priority

        if len(update_args) > 1:
            commands.append(
                {
                    "type": "item_update",
                    "uuid": str(uuid.uuid4()),
                    "args": update_args,
                }
            )

        if project_id is not None and project_id != existing_task.project_id:
            commands.append(
                {
                    "type": "item_move",
                    "uuid": str(uuid.uuid4()),
                    "args": {"id": task_id, "project_id": project_id},
                }
            )

        if not commands:
            return

        data = await self._post_sync(commands=commands)
        self._raise_on_sync_errors(data)

    async def close_task(self, task_id: str) -> None:
        resp = await self._client.post(f"/tasks/{task_id}/close")
        resp.raise_for_status()

    async def sync_incremental(self, sync_token: str) -> tuple[str, list[TodoistTask]]:
        data = await self._post_sync(
            sync_token=sync_token,
            resource_types=["items"],
        )
        raw_new_token = data.get("sync_token")
        new_token = str(raw_new_token) if raw_new_token else sync_token
        items = [TodoistTask.model_validate(raw) for raw in (data.get("items") or [])]
        return new_token, items

    async def get_completed_activities(
        self,
        since_iso: str,
        until_iso: str,
    ) -> list[TodoistActivityEvent]:
        """Query Todoist Activity Log for completed tasks (including recurring instances)."""
        events: list[TodoistActivityEvent] = []
        cursor: str | None = None
        while True:
            params: dict[str, str | int] = {
                "object_type": "item",
                "event_type": "completed",
                "date_from": since_iso,
                "date_to": until_iso,
                "limit": 100,
            }
            if cursor:
                params["cursor"] = cursor
            resp = await self._client.get("/activities", params=params)
            resp.raise_for_status()
            payload = resp.json()
            page_events = payload.get("results")
            if page_events is None:
                page_events = payload.get("events") or []
            for raw in page_events:
                parsed = TodoistActivityEvent.from_api_event(raw)
                if parsed is not None:
                    events.append(parsed)
            cursor = payload.get("next_cursor")
            if not cursor:
                break
        return events

    async def _post_sync(
        self,
        *,
        sync_token: str | None = None,
        resource_types: Sequence[str] | None = None,
        commands: Sequence[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        form: dict[str, str] = {}
        if sync_token is not None:
            form["sync_token"] = sync_token
        if resource_types is not None:
            form["resource_types"] = json.dumps(list(resource_types))
        if commands is not None:
            form["commands"] = json.dumps(list(commands))
        resp = await self._client.post("/sync", data=form)
        resp.raise_for_status()
        payload: dict[str, Any] = resp.json()
        return payload

    @staticmethod
    def _raise_on_sync_errors(payload: dict[str, Any]) -> None:
        sync_status = payload.get("sync_status") or {}
        failures = {
            cmd_id: status for cmd_id, status in sync_status.items() if status != "ok"
        }
        if failures:
            raise TodoistAPIError(f"Todoist /sync command failure: {failures}")
