"""Helpers for text and multimodal chat message content."""

from __future__ import annotations

from typing import Any


def content_text(content: Any, *, include_media_labels: bool = True) -> str:
    """Extract readable text from OpenAI-compatible string or content-part payloads."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")

    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = str(part.get("type", ""))
        if part_type in {"text", "input_text"}:
            text = part.get("text")
            if text:
                parts.append(str(text))
        elif include_media_labels and part_type in {"image_url", "input_image"}:
            parts.append("[image]")
        elif include_media_labels and part_type in {"file", "input_file"}:
            parts.append("[file]")
    return "\n".join(parts)


def user_content(text: str, image_parts: list[dict] | None = None) -> str | list[dict]:
    """Build Chat Completions-compatible user content."""
    images = list(image_parts or [])
    if not images:
        return text
    return [{"type": "text", "text": text}, *images]
