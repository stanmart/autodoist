from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from autodoist.config import RulesConfig
from autodoist.db import CompletionRecord, Database
from autodoist.models import (
    CaptureItem,
    CaptureResult,
    CaptureTaskInput,
    CompleteTaskResult,
    CompletionHistoryItem,
    DropTaskResult,
    PostHocLoggedOut,
    SkippedDuplicateOut,
    TaskList,
    TaskOut,
    UpdateTaskResult,
    api_to_app_priority,
    app_to_api_priority,
    normalize_text,
)
from autodoist.todoist import AmbiguousProjectError, TodoistClient

_TASK_ID_RE = re.compile(r"[A-Za-z0-9_-]+")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

DueFilter = Literal["all", "overdue", "today", "week", "none"]
GroupBy = Literal["label", "project", "due", "none"]

# "today" and "week" include overdue tasks: they are still waiting to be done.
_DUE_FILTER_BUCKETS: dict[str, frozenset[str]] = {
    "all": frozenset({"overdue", "today", "this_week", "later", "no_date"}),
    "overdue": frozenset({"overdue"}),
    "today": frozenset({"overdue", "today"}),
    "week": frozenset({"overdue", "today", "this_week"}),
    "none": frozenset({"no_date"}),
}


def _due_bucket(due_day: str | None, today: date) -> str:
    if due_day is None:
        return "no_date"
    today_str = today.isoformat()
    if due_day < today_str:
        return "overdue"
    if due_day == today_str:
        return "today"
    if due_day <= (today + timedelta(days=7)).isoformat():
        return "this_week"
    return "later"


def _age_days(added_at: str | None, now: datetime) -> int | None:
    if not added_at:
        return None
    try:
        return max((now - datetime.fromisoformat(added_at)).days, 0)
    except (ValueError, TypeError):
        return None


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
        self._allowed_labels = set(rules.allowed_labels)

    def _require_unambiguous(self, project_name: str) -> None:
        """Fail closed when an allowlisted name matches several Todoist projects."""
        if self.todoist.is_ambiguous_project_name(project_name):
            raise AmbiguousProjectError(project_name)

    async def _require_allowed_task_project(self, task_id: str, project_id: str) -> str:
        project_name = await self.todoist.resolve_project_name(project_id)
        if project_name is None or project_name not in self._allowed_projects:
            raise ValueError(
                f"Task {task_id!r} belongs to project {project_name!r}, "
                "which is outside allowed_projects"
            )
        self._require_unambiguous(project_name)
        return project_name

    async def get_tasks(
        self,
        *,
        project: str | None = None,
        label: str | None = None,
        due: DueFilter = "all",
        slot: str | None = None,
        needs_triage: bool = False,
        older_than_days: int | None = None,
        group_by: GroupBy = "label",
        limit: int = 50,
    ) -> TaskList:
        if project is not None and project not in self._allowed_projects:
            raise ValueError(
                f"Project {project!r} is not allowed; "
                f"allowed projects: {self.rules.allowed_projects}"
            )
        if label is not None:
            _validate_labels([label], self.rules.allowed_labels)
        routine = None
        if slot is not None:
            routine = next((r for r in self.rules.routines if r.name == slot), None)
            if routine is None:
                raise ValueError(
                    f"Unknown slot {slot!r}; "
                    f"configured slots: {[r.name for r in self.rules.routines]}"
                )

        project_id = await self.todoist.get_project_id(project) if project else None
        tasks = await self.todoist.get_active_tasks(project_id=project_id, label=label)

        now = datetime.now(ZoneInfo(self.rules.timezone))
        today = now.date()
        someday = self.rules.someday_label
        review = self.rules.review_label
        slot_due_limit = (
            (today + timedelta(days=routine.due_within_days)).isoformat()
            if routine is not None and routine.due_within_days is not None
            else None
        )

        matched: list[tuple[TaskOut, str]] = []
        for task in tasks:
            if task.project_name is None or task.project_name not in self._allowed_projects:
                continue
            self._require_unambiguous(task.project_name)

            # Someday tasks stay out of the way unless they are asked for by label.
            if someday is not None and someday in task.labels and label != someday:
                continue
            due_raw = task.due.date if task.due is not None else None
            due_day = due_raw[:10] if due_raw else None
            bucket = _due_bucket(due_day, today)
            if bucket not in _DUE_FILTER_BUCKETS[due]:
                continue
            if routine is not None and not (
                routine.name in task.labels
                or (slot_due_limit is not None and due_day is not None
                    and due_day <= slot_due_limit)
            ):
                continue
            if needs_triage and not (
                task.project_name == self.rules.inbox_project
                or (review is not None and review in task.labels)
            ):
                continue
            age_days = _age_days(task.added_at, now)
            if older_than_days is not None and (age_days is None or age_days < older_than_days):
                continue

            matched.append(
                (
                    TaskOut(
                        id=task.id,
                        content=task.content,
                        project=task.project_name,
                        labels=task.labels,
                        priority=api_to_app_priority(task.priority),
                        due_date=due_raw,
                        is_recurring=task.due.is_recurring if task.due is not None else False,
                        notes=task.description or None,
                        age_days=age_days,
                    ),
                    bucket,
                )
            )

        matched.sort(key=lambda pair: (pair[0].priority, pair[0].due_date or "9999-99-99"))
        groups: dict[str, list[TaskOut]] = {}
        for out, bucket in matched[:limit]:
            if group_by == "label":
                key = next(
                    (lbl for lbl in self.rules.synergy_labels if lbl in out.labels),
                    "untagged",
                )
            elif group_by == "project":
                key = out.project
            elif group_by == "due":
                key = bucket
            else:
                key = "tasks"
            groups.setdefault(key, []).append(out)

        return TaskList(
            groups=groups,
            total=len(matched),
            truncated=len(matched) > limit,
        )

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
                review = self.rules.review_label
                labels_with_review = list(
                    dict.fromkeys([*clean_labels, *([review] if review else [])])
                )
                batch_inputs.append(
                    CaptureTaskInput(
                        content=item.content,
                        project=item.project,
                        labels=labels_with_review,
                        due_string=item.due_string,
                        priority=app_to_api_priority(item.priority),
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
        due_string: str | None = None,
        content: str | None = None,
        notes: str | None = None,
        labels: list[str] | None = None,
        priority: int | None = None,
        project: str | None = None,
        clear_review_flag: bool = False,
    ) -> UpdateTaskResult:
        _validate_task_id(task_id)
        if content is not None:
            content = content.strip()
            if not content:
                raise ValueError("content must not be empty")
        if due_string is not None:
            due_string = due_string.strip()
            if not due_string:
                raise ValueError("due_string must not be empty")
            if new_due_date is not None:
                raise ValueError("Pass either new_due_date or due_string, not both")
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
        await self._require_allowed_task_project(task_id, existing.project_id)

        final_labels: list[str] | None = None
        if clean_labels is not None or clear_review_flag:
            if clean_labels is not None:
                # `labels` replaces the managed labels only; labels outside the allowlist
                # were set by hand in Todoist and are not ours to remove.
                unmanaged = [lbl for lbl in existing.labels if lbl not in self._allowed_labels]
                base_labels = list(dict.fromkeys([*clean_labels, *unmanaged]))
            else:
                base_labels = list(existing.labels)
            review = self.rules.review_label
            if clear_review_flag:
                base_labels = [lbl for lbl in base_labels if lbl != review]
            elif review in existing.labels and review not in base_labels:
                base_labels.append(review)
            final_labels = base_labels

        api_priority = app_to_api_priority(priority) if priority is not None else None
        target_project_id = await self.todoist.get_project_id(project) if project else None
        await self.todoist.update_task(
            task_id,
            existing,
            new_due_date=new_due_date,
            due_string=due_string,
            content=content,
            description=notes,
            labels=final_labels,
            priority=api_priority,
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
        project_name = await self._require_allowed_task_project(task_id, existing.project_id)
        result = CompleteTaskResult(
            task_id=task_id,
            content=existing.content,
            project=project_name,
            completed=True,
        )
        if existing.checked:
            # Already completed: closing and logging again would duplicate the history row.
            return result

        await self.todoist.close_task(task_id)
        await asyncio.to_thread(
            self.db.record_completion,
            todoist_task_id=task_id,
            content=existing.content,
            project_name=project_name,
            labels=existing.labels,
            source="mcp_complete",
        )
        return result

    async def drop_task(self, task_id: str) -> DropTaskResult:
        _validate_task_id(task_id)
        existing = await self.todoist.get_task(task_id)
        project_name = await self._require_allowed_task_project(task_id, existing.project_id)

        # Deleting a parent deletes its subtasks too, and those would not be snapshotted.
        siblings = await self.todoist.get_active_tasks(project_id=existing.project_id)
        if any(t.parent_id == task_id for t in siblings):
            raise ValueError(
                f"Task {task_id!r} has subtasks; drop or move them before dropping it"
            )

        # Snapshot first: a leftover snapshot of a task that survived is harmless, a
        # deleted task without one is gone for good.
        existing.project_name = project_name
        await asyncio.to_thread(
            self.db.record_dropped_task,
            todoist_task_id=task_id,
            content=existing.content,
            project_name=project_name,
            task_json=existing.model_dump_json(),
        )
        await self.todoist.delete_task(task_id)
        return DropTaskResult(
            task_id=task_id,
            content=existing.content,
            project=project_name,
            dropped=True,
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
