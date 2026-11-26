from __future__ import annotations

import time
import uuid
from typing import Any

import httpx
from pydantic import BaseModel, Field

TODOIST_API_V1_BASE = "https://api.todoist.com/api/v1"


class TodoistAPIError(RuntimeError):
    """Raised when Todoist returns an application-level /sync command error."""


class TaskSummary(BaseModel):
    id: str
    content: str
    project_id: str
    project_name: str | None = None
    labels: list[str] = Field(default_factory=list)
    priority: int = 1
    due_date: str | None = None
    is_recurring: bool = False


class CaptureTaskInput(BaseModel):
    content: str
    project: str
    labels: list[str] = Field(default_factory=list)
    due_string: str | None = None
    priority: int = 1
    description: str = ""


def _parse_task_summary(raw: dict[str, Any], project_name: str | None = None) -> TaskSummary:
    due = raw.get("due") or {}
    return TaskSummary(
        id=str(raw["id"]),
        content=str(raw.get("content") or ""),
        project_id=str(raw.get("project_id") or ""),
        project_name=project_name,
        labels=[str(lbl) for lbl in (raw.get("labels") or [])],
        priority=int(raw.get("priority") or 1),
        due_date=due.get("date") if isinstance(due, dict) else None,
        is_recurring=bool(due.get("is_recurring", False)) if isinstance(due, dict) else False,
    )


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
        self._projects_ttl_seconds = projects_ttl_seconds
        self._projects_loaded_at = 0.0
        self._name_to_id: dict[str, str] = {}
        self._id_to_name: dict[str, str] = {}

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def update_projects_cache(self, projects: list[dict[str, Any]]) -> None:
        for proj in projects:
            pid = str(proj["id"])
            old_name = self._id_to_name.get(pid)
            if proj.get("is_deleted") or proj.get("is_archived"):
                self._id_to_name.pop(pid, None)
                if old_name and self._name_to_id.get(old_name) == pid:
                    self._name_to_id.pop(old_name, None)
                continue
            name = str(proj["name"])
            if old_name and old_name != name and self._name_to_id.get(old_name) == pid:
                self._name_to_id.pop(old_name, None)
            self._name_to_id[name] = pid
            self._id_to_name[pid] = name

    async def ensure_projects_loaded(self, *, force_refresh: bool = False) -> bool:
        cache_expired = (time.monotonic() - self._projects_loaded_at) >= self._projects_ttl_seconds
        if self._name_to_id and not force_refresh and not cache_expired:
            return False
        projects: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, str] = {}
            if cursor:
                params["cursor"] = cursor
            resp = await self._client.get("/projects", params=params)
            resp.raise_for_status()
            payload = resp.json()
            projects.extend(payload.get("results") or [])
            cursor = payload.get("next_cursor")
            if not cursor:
                break
        self._name_to_id.clear()
        self._id_to_name.clear()
        self.update_projects_cache(projects)
        self._projects_loaded_at = time.monotonic()
        return True

    async def get_project_id(self, name: str) -> str:
        refreshed = await self.ensure_projects_loaded()
        if name not in self._name_to_id and not refreshed:
            await self.ensure_projects_loaded(force_refresh=True)
        if name not in self._name_to_id:
            raise ValueError(f"Project {name!r} not found in Todoist")
        return self._name_to_id[name]

    def get_project_name(self, project_id: str) -> str | None:
        return self._id_to_name.get(project_id)

    async def resolve_project_name(self, project_id: str) -> str | None:
        if not project_id:
            return None
        refreshed = await self.ensure_projects_loaded()
        name = self._id_to_name.get(project_id)
        if name is None and not refreshed:
            await self.ensure_projects_loaded(force_refresh=True)
            name = self._id_to_name.get(project_id)
        return name

    async def get_active_tasks(
        self,
        *,
        project_id: str | None = None,
        label: str | None = None,
    ) -> list[TaskSummary]:
        await self.ensure_projects_loaded()
        tasks: list[TaskSummary] = []
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
                pid = str(raw.get("project_id") or "")
                tasks.append(_parse_task_summary(raw, self.get_project_name(pid)))
            cursor = payload.get("next_cursor")
            if not cursor:
                break
        return tasks

    async def get_task(self, task_id: str) -> dict[str, Any]:
        resp = await self._client.get(f"/tasks/{task_id}")
        resp.raise_for_status()
        return dict(resp.json())

    async def batch_add_tasks(self, items: list[CaptureTaskInput]) -> list[dict[str, str]]:
        if not items:
            return []
        await self.ensure_projects_loaded()

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

            cmd_uuid = str(uuid.uuid4())
            temp_id = str(uuid.uuid4())
            commands.append(
                {
                    "type": "item_add",
                    "temp_id": temp_id,
                    "uuid": cmd_uuid,
                    "args": args,
                }
            )
            temp_meta.append((temp_id, item))

        resp = await self._client.post("/sync", json={"commands": commands})
        resp.raise_for_status()
        data = resp.json()
        self._raise_on_sync_errors(data)

        mapping = data.get("temp_id_mapping", {})
        return [
            {
                "id": str(mapping.get(temp_id, "")),
                "content": item.content,
                "project": item.project,
            }
            for temp_id, item in temp_meta
        ]

    async def update_task(
        self,
        task_id: str,
        existing_task: dict[str, Any],
        *,
        new_due_date: str | None = None,
        labels: list[str] | None = None,
        priority: int | None = None,
        project_id: str | None = None,
    ) -> None:
        commands: list[dict[str, Any]] = []
        update_args: dict[str, Any] = {"id": task_id}

        if new_due_date is not None:
            existing_due = existing_task.get("due")
            if (
                isinstance(existing_due, dict)
                and existing_due.get("is_recurring")
                and existing_due.get("string")
            ):
                due_payload: dict[str, Any] = {
                    "date": new_due_date,
                    "string": existing_due["string"],
                    "is_recurring": True,
                }
                if existing_due.get("lang"):
                    due_payload["lang"] = existing_due["lang"]
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

        if project_id is not None and project_id != str(existing_task.get("project_id", "")):
            commands.append(
                {
                    "type": "item_move",
                    "uuid": str(uuid.uuid4()),
                    "args": {"id": task_id, "project_id": project_id},
                }
            )

        if not commands:
            return

        resp = await self._client.post("/sync", json={"commands": commands})
        resp.raise_for_status()
        self._raise_on_sync_errors(resp.json())

    async def close_task(self, task_id: str) -> None:
        resp = await self._client.post(f"/tasks/{task_id}/close")
        resp.raise_for_status()

    async def sync_incremental(self, sync_token: str) -> dict[str, Any]:
        resp = await self._client.post(
            "/sync",
            json={
                "sync_token": sync_token,
                "resource_types": ["items", "projects"],
            },
        )
        resp.raise_for_status()
        data = dict(resp.json())
        if "projects" in data and isinstance(data["projects"], list):
            self.update_projects_cache(data["projects"])
            self._projects_loaded_at = time.monotonic()
        return data

    async def get_completed_activities(
        self,
        since_iso: str,
        until_iso: str,
    ) -> list[dict[str, Any]]:
        """Query Todoist Activity Log for completed tasks (including recurring instances)."""
        events: list[dict[str, Any]] = []
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
            events.extend(page_events)
            cursor = payload.get("next_cursor")
            if not cursor:
                break
        return events

    @staticmethod
    def _raise_on_sync_errors(payload: dict[str, Any]) -> None:
        sync_status = payload.get("sync_status") or {}
        failures = {
            cmd_id: status for cmd_id, status in sync_status.items() if status != "ok"
        }
        if failures:
            raise TodoistAPIError(f"Todoist /sync command failure: {failures}")
