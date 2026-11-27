from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def api_to_app_priority(api_priority: int) -> int:
    """Convert Todoist API priority (4=p1 highest .. 1=p4 normal) to app scale (1=p1 .. 4=p4)."""
    if not (1 <= api_priority <= 4):
        raise ValueError("priority must be between 1 and 4")
    return 5 - api_priority


def app_to_api_priority(app_priority: int) -> int:
    """Convert app priority (1=p1 highest .. 4=p4 normal) to Todoist API scale (4=p1 .. 1=p4)."""
    if not (1 <= app_priority <= 4):
        raise ValueError("priority must be between 1 and 4")
    return 5 - app_priority


class TodoistDue(BaseModel):
    date: str | None = None
    string: str | None = None
    is_recurring: bool = False
    lang: str | None = None
    timezone: str | None = None


class TodoistProject(BaseModel):
    id: str
    name: str
    is_deleted: bool = False
    is_archived: bool = False

    @field_validator("id", mode="before")
    @classmethod
    def coerce_id(cls, value: object) -> str:
        return str(value)


class TodoistTask(BaseModel):
    id: str
    content: str = ""
    description: str = ""
    project_id: str = ""
    project_name: str | None = None
    parent_id: str | None = None
    labels: list[str] = Field(default_factory=list)
    priority: int = 1
    due: TodoistDue | None = None
    checked: bool = False
    is_deleted: bool = False
    added_at: str | None = None
    completed_at: str | None = None

    @field_validator("id", "project_id", mode="before")
    @classmethod
    def coerce_ids(cls, value: object) -> str:
        return "" if value is None else str(value)

    @field_validator("parent_id", mode="before")
    @classmethod
    def coerce_parent_id(cls, value: object) -> str | None:
        return None if value is None else str(value)

    @field_validator("content", "description", mode="before")
    @classmethod
    def coerce_content(cls, value: object) -> str:
        return "" if value is None else str(value)

    @field_validator("labels", mode="before")
    @classmethod
    def coerce_labels(cls, value: object) -> list[str]:
        if value is None:
            return []
        return [str(lbl) for lbl in value] if isinstance(value, list) else []

    @field_validator("priority", mode="before")
    @classmethod
    def coerce_priority(cls, value: object) -> int:
        return 1 if value is None else int(value)  # type: ignore[arg-type]


class TodoistActivityEvent(BaseModel):
    task_id: str
    project_id: str
    content: str = ""
    completed_at: str | None = None

    @classmethod
    def from_api_event(cls, raw: dict[str, Any]) -> TodoistActivityEvent | None:
        extra = raw.get("extra_data") or {}
        task_id = raw.get("object_id") or extra.get("item_id")
        if not task_id:
            return None
        project_id = raw.get("parent_project_id") or extra.get("project_id") or ""
        return cls(
            task_id=str(task_id),
            project_id=str(project_id),
            content=str(extra.get("content") or raw.get("content") or ""),
            completed_at=raw.get("event_date"),
        )


class CaptureItem(BaseModel):
    content: str = Field(min_length=1)
    mode: Literal["task", "post_hoc"] = "task"
    project: str
    labels: list[str] = Field(default_factory=list)
    due_string: str | None = None
    priority: int = Field(
        default=4,
        ge=1,
        le=4,
        description="Todoist priority: 1 (p1, highest) to 4 (p4, normal/default).",
    )
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


class CaptureTaskInput(BaseModel):
    content: str
    project: str
    labels: list[str] = Field(default_factory=list)
    due_string: str | None = None
    priority: int = 1
    description: str = ""


class TaskOut(BaseModel):
    id: str
    content: str
    project: str
    labels: list[str]
    priority: int = Field(
        ge=1,
        le=4,
        description="Todoist priority: 1 (p1, highest) to 4 (p4, normal).",
    )
    due_date: str | None = None
    is_recurring: bool = False
    notes: str | None = None
    age_days: int | None = Field(default=None, description="Days since the task was created.")


class TaskList(BaseModel):
    groups: dict[str, list[TaskOut]]
    total: int = Field(description="Tasks matching the filters, before `limit` was applied.")
    truncated: bool


class CreatedTaskOut(BaseModel):
    id: str
    content: str
    project: str


class SkippedDuplicateOut(BaseModel):
    content: str
    project: str


class PostHocLoggedOut(BaseModel):
    content: str
    project: str


class CaptureResult(BaseModel):
    created: list[CreatedTaskOut] = Field(default_factory=list)
    skipped_duplicates: list[SkippedDuplicateOut] = Field(default_factory=list)
    logged_post_hoc: list[PostHocLoggedOut] = Field(default_factory=list)


class UpdateTaskResult(BaseModel):
    task_id: str
    updated: bool
    preserved_recurrence: bool


class CompleteTaskResult(BaseModel):
    task_id: str
    content: str
    project: str
    completed: bool


class DropTaskResult(BaseModel):
    task_id: str
    content: str
    project: str
    dropped: bool


class CompletionHistoryItem(BaseModel):
    content: str
    project_name: str
    completed_at: str


class CompletionHistory(BaseModel):
    completions: list[CompletionHistoryItem]
