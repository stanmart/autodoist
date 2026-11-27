from __future__ import annotations

from autodoist.config import RulesConfig


def build_instructions(rules: RulesConfig) -> str:
    """Render the MCP server instructions: the configured vocabulary plus house rules."""
    slots = [routine.name for routine in rules.routines]
    lines = [
        "autodoist manages a household's Todoist tasks. Use it for any task, chore, "
        "errand, or reminder request.",
        "",
        f"Projects: {', '.join(rules.allowed_projects)}. "
        f"New tasks may be created in: {', '.join(rules.capture_projects)}.",
        f"{rules.inbox_project} holds raw, hand-captured items that still need triage.",
        "",
        "Labels (the only ones you may set):",
    ]
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
            "Slot times are calendar events named after the slot. "
            "Use get_tasks(slot=...) for what to do in one."
        )

    slot_rule = "exactly one slot label"
    if rules.someday_label:
        slot_rule += f" or {rules.someday_label}"
    lines += [
        "",
        "Rules:",
        "- Priority runs from 1 (highest) to 4 (default).",
        "- A due date is a real deadline or a recurrence, never just a plan to do it that day.",
        "- Triage: get_tasks(needs_triage=true), then for each task rewrite the content as a "
        f"clear action, set labels and {slot_rule if rules.routines else 'a due date if any'}, "
        f"and move it out of {rules.inbox_project}. Respect slot capacity: push overflow to "
        "a later slot and say so. Pass clear_review_flag=true only once the user has agreed.",
        "- Something already done: complete_task if a matching active task exists, otherwise "
        "capture_items with mode='post_hoc'.",
        "- Something that will not be done: drop_task. Never complete it, because "
        "completions feed the history used to spot recurring needs.",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    from autodoist.config import load_config

    print(build_instructions(load_config()[0].rules))
