from __future__ import annotations

import asyncio
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from autodoist.config import RulesConfig
from autodoist.db import CompletionRecord, Database
from autodoist.models import (
    CaptureItem,
    CaptureResult,
    CaptureTaskInput,
    CompleteTaskResult,
    CompletionHistoryItem,
    PostHocLoggedOut,
    SkippedDuplicateOut,
    TaskOut,
    UpdateTaskResult,
    normalize_text,
)
from autodoist.todoist import TodoistClient

_TASK_ID_RE = re.compile(r"[A-Za-z0-9_-]+")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


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


class AutodoistService:
    """Domain service enforcing project/label guardrails and orchestrating Todoist + SQLite."""

    def __init__(
        self,
        rules: RulesConfig,
        db: Database,
        todoist: TodoistClient,
    ) -> None:
        self.rules = rules
        self.db = db
        self.todoist = todoist
        self._allowed_projects = set(rules.allowed_projects)
        self._capture_projects = set(rules.capture_projects)

    async def get_tasks_by_synergy(
        self,
        *,
        project: str | None = None,
        label: str | None = None,
        include_overdue: bool = True,
    ) -> dict[str, list[TaskOut]]:
        if project is not None and project not in self._allowed_projects:
            raise ValueError(
                f"Project {project!r} is not allowed; "
                f"allowed projects: {self.rules.allowed_projects}"
            )
        if label is not None:
            _validate_labels([label], self.rules.allowed_labels)

        project_id = await self.todoist.get_project_id(project) if project else None
        tasks = await self.todoist.get_active_tasks(project_id=project_id, label=label)

        today_str = datetime.now(ZoneInfo(self.rules.timezone)).strftime("%Y-%m-%d")
        groups: dict[str, list[TaskOut]] = {lbl: [] for lbl in self.rules.synergy_labels}
        groups["untagged"] = []

        for task in tasks:
            if task.project_name is None or task.project_name not in self._allowed_projects:
                continue
            due_date = task.due.date if task.due is not None else None
            if not include_overdue and due_date and due_date < today_str:
                continue
            bucket = next(
                (lbl for lbl in self.rules.synergy_labels if lbl in task.labels),
                "untagged",
            )
            groups[bucket].append(
                TaskOut(
                    id=task.id,
                    content=task.content,
                    project=task.project_name,
                    labels=task.labels,
                    priority=task.priority,
                    due_date=due_date,
                    is_recurring=task.due.is_recurring if task.due is not None else False,
                )
            )

        for bucket_items in groups.values():
            bucket_items.sort(
                key=lambda t: (-t.priority, t.due_date or "9999-99-99")
            )

        return {k: v for k, v in groups.items() if v}

    async def capture_items(self, items: list[CaptureItem]) -> CaptureResult:
        if not items:
            return CaptureResult()

        validated: list[tuple[CaptureItem, list[str]]] = []
        for item in items:
            if item.project not in self._capture_projects:
                raise ValueError(
                    f"Project {item.project!r} is not allowed for capture; "
                    f"allowed capture projects: {self.rules.capture_projects}"
                )
            clean_labels = _validate_labels(item.labels, self.rules.allowed_labels)
            validated.append((item, clean_labels))

        task_candidates: list[tuple[CaptureItem, list[str]]] = []
        post_hoc_items: list[tuple[CaptureItem, list[str]]] = []
        for item, clean_labels in validated:
            if item.mode == "post_hoc":
                post_hoc_items.append((item, clean_labels))
            else:
                task_candidates.append((item, clean_labels))

        created = []
        skipped_duplicates: list[SkippedDuplicateOut] = []

        if task_candidates:
            existing_by_project: dict[str, set[str]] = {}
            target_projects = sorted({item.project for item, _ in task_candidates})
            for proj_name in target_projects:
                pid = await self.todoist.get_project_id(proj_name)
                active = await self.todoist.get_active_tasks(project_id=pid)
                existing_by_project[proj_name] = {
                    normalize_text(t.content) for t in active
                }

            batch_inputs: list[CaptureTaskInput] = []
            for item, clean_labels in task_candidates:
                norm = normalize_text(item.content)
                proj_seen = existing_by_project[item.project]
                if norm in proj_seen:
                    skipped_duplicates.append(
                        SkippedDuplicateOut(content=item.content, project=item.project)
                    )
                    continue
                proj_seen.add(norm)
                labels_with_review = list(dict.fromkeys([*clean_labels, self.rules.review_label]))
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
                created = await self.todoist.batch_add_tasks(batch_inputs)

        post_hoc_records: list[CompletionRecord] = []
        logged_post_hoc: list[PostHocLoggedOut] = []
        seen_post_hoc: set[tuple[str, str]] = set()
        for item, clean_labels in post_hoc_items:
            norm = normalize_text(item.content)
            key = (item.project, norm)
            is_dup = key in seen_post_hoc or await asyncio.to_thread(
                self.db.has_recent_post_hoc,
                item.content,
                item.project,
            )
            if is_dup:
                skipped_duplicates.append(
                    SkippedDuplicateOut(content=item.content, project=item.project)
                )
                continue
            seen_post_hoc.add(key)
            post_hoc_records.append(
                CompletionRecord(
                    todoist_task_id=None,
                    content=item.content,
                    project_name=item.project,
                    labels=clean_labels,
                    source="post_hoc_log",
                )
            )
            logged_post_hoc.append(PostHocLoggedOut(content=item.content, project=item.project))

        if post_hoc_records:
            await asyncio.to_thread(self.db.record_completions, post_hoc_records)

        return CaptureResult(
            created=created,
            skipped_duplicates=skipped_duplicates,
            logged_post_hoc=logged_post_hoc,
        )

    async def update_or_reschedule(
        self,
        *,
        task_id: str,
        new_due_date: str | None = None,
        labels: list[str] | None = None,
        priority: int | None = None,
        project: str | None = None,
        clear_review_flag: bool = False,
    ) -> UpdateTaskResult:
        _validate_task_id(task_id)
        if new_due_date is not None:
            if not _ISO_DATE_RE.fullmatch(new_due_date):
                raise ValueError("new_due_date must use YYYY-MM-DD format")
            date.fromisoformat(new_due_date)
        if priority is not None and not (1 <= priority <= 4):
            raise ValueError("priority must be between 1 and 4")
        if project is not None and project not in self._allowed_projects:
            raise ValueError(
                f"Project {project!r} is not allowed; "
                f"allowed projects: {self.rules.allowed_projects}"
            )
        clean_labels = (
            _validate_labels(labels, self.rules.allowed_labels)
            if labels is not None
            else None
        )

        existing = await self.todoist.get_task(task_id)
        current_project = await self.todoist.resolve_project_name(existing.project_id)
        if current_project not in self._allowed_projects:
            raise ValueError(
                f"Task {task_id!r} belongs to project {current_project!r}, "
                "which is outside allowed_projects"
            )

        final_labels: list[str] | None = None
        if clean_labels is not None or clear_review_flag:
            base_labels = (
                list(clean_labels) if clean_labels is not None else list(existing.labels)
            )
            if clear_review_flag:
                base_labels = [lbl for lbl in base_labels if lbl != self.rules.review_label]
            elif (
                self.rules.review_label in existing.labels
                and self.rules.review_label not in base_labels
            ):
                base_labels.append(self.rules.review_label)
            final_labels = base_labels

        target_project_id = await self.todoist.get_project_id(project) if project else None
        await self.todoist.update_task(
            task_id,
            existing,
            new_due_date=new_due_date,
            labels=final_labels,
            priority=priority,
            project_id=target_project_id,
        )

        due = existing.due
        preserved_recurrence = bool(
            new_due_date and due is not None and due.is_recurring and due.string
        )
        return UpdateTaskResult(
            task_id=task_id,
            updated=True,
            preserved_recurrence=preserved_recurrence,
        )

    async def complete_task(self, task_id: str) -> CompleteTaskResult:
        _validate_task_id(task_id)
        existing = await self.todoist.get_task(task_id)
        project_name = await self.todoist.resolve_project_name(existing.project_id)
        if project_name is None or project_name not in self._allowed_projects:
            raise ValueError(
                f"Task {task_id!r} belongs to project {project_name!r}, "
                "which is outside allowed_projects"
            )

        await self.todoist.close_task(task_id)
        await asyncio.to_thread(
            self.db.record_completion,
            todoist_task_id=task_id,
            content=existing.content,
            project_name=project_name,
            labels=existing.labels,
            source="mcp_complete",
        )
        return CompleteTaskResult(
            task_id=task_id,
            content=existing.content,
            project=project_name,
            completed=True,
        )

    async def get_completion_history(
        self,
        *,
        days: int = 90,
        project: str | None = None,
    ) -> list[CompletionHistoryItem]:
        if project is not None and project not in self._allowed_projects:
            raise ValueError(
                f"Project {project!r} is not allowed; "
                f"allowed projects: {self.rules.allowed_projects}"
            )
        return await asyncio.to_thread(
            self.db.get_completion_history,
            days=days,
            project=project,
            allowed_projects=self.rules.allowed_projects,
        )
