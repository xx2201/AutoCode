"""Todo and planning helpers."""

from __future__ import annotations


def normalize_todos(items: list[dict]) -> list[dict]:
    todos = []
    for item in items:
        content = str(item.get("content", "")).strip()
        status = str(item.get("status", "pending")).strip().lower()
        if not content:
            continue
        if status not in {"pending", "in_progress", "completed", "blocked"}:
            status = "pending"
        todos.append({"content": content, "status": status})
    return todos


def render_todos(items: list[dict]) -> str:
    if not items:
        return "- (no todo items yet)"
    lines = []
    for item in items:
        status = item.get("status", "pending")
        marker = {
            "pending": "[ ]",
            "in_progress": "[~]",
            "completed": "[x]",
            "blocked": "[!]",
        }.get(status, "[ ]")
        lines.append(f"{marker} {item.get('content', '')}")
    return "\n".join(lines)
