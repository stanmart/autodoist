from __future__ import annotations

from autodoist.config import RulesConfig


def build_instructions(rules: RulesConfig) -> str:
    """Render the MCP server instructions: the configured vocabulary, and nothing else.

    Procedures (triage, reviews, briefs) belong in client-side skills, not here.
    """
    slots = [routine.name for routine in rules.routines]
    lines = [
        "autodoist manages a household's tasks in Todoist.",
        "",
        f"Projects (new tasks may be created in: {', '.join(rules.capture_projects)}):",
    ]
    for project in rules.allowed_projects:
        description = rules.project_descriptions.get(project)
        if description is None and project == rules.inbox_project:
            description = "raw, hand-captured items that still need triage"
        lines.append(f"- {project}: {description}" if description else f"- {project}")
    lines += ["", "Labels (the only ones you may set):"]
    for label in rules.allowed_labels:
        if label in slots:
            continue
        description = rules.label_descriptions.get(label)
        if description is None and label == rules.review_label:
            description = "set automatically on tasks you create; not yet confirmed by a human"
        if description is None and label == rules.someday_label:
            description = "no plan to do it soon; hidden from get_tasks unless asked for"
        lines.append(f"- {label}: {description}" if description else f"- {label}")

    if rules.routines:
        lines += ["", "Routine slots (when a task gets done; the slot name is its label):"]
        for routine in rules.routines:
            parts = [routine.purpose, f"Capacity: {routine.capacity}" if routine.capacity else ""]
            detail = ". ".join(part.rstrip(".") for part in parts if part)
            lines.append(f"- {routine.name}: {detail}" if detail else f"- {routine.name}")
        lines.append(
            "Slot times are calendar events named after the slot; "
            "get_tasks(slot=...) lists what belongs in one."
        )

    lines += ["", "Priority runs from 1 (highest) to 4 (default)."]
    return "\n".join(lines)


if __name__ == "__main__":
    from autodoist.config import load_config

    print(build_instructions(load_config()[0].rules))
