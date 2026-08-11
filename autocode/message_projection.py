"""Serialize canonical agent history into provider-native wire messages."""

from __future__ import annotations

import base64
import json
import re
from typing import Any

from .message_content import is_internal_visual_context, user_content


_DATA_URL = re.compile(r"^data:(?P<media_type>[^;,]+);base64,(?P<data>.+)$", re.DOTALL)


def serialize_chat_completions(
    system_prompt: str,
    history: list[dict],
) -> list[dict]:
    """Translate canonical history into strict Chat Completions messages."""
    projected: list[dict] = [{"role": "system", "content": system_prompt}]
    pending_media: list[tuple[str, dict]] = []

    def flush_tool_media() -> None:
        if not pending_media:
            return
        labels = ", ".join(dict.fromkeys(source for source, _ in pending_media))
        unique: list[dict] = []
        identities: set[tuple[str, str]] = set()
        for _, item in pending_media:
            identity = model_content_identity(item)
            if identity in identities:
                continue
            identities.add(identity)
            unique.append(dict(item))
        projected.append(
            {
                "role": "user",
                "content": user_content(
                    f"Visual content returned by tools: {labels}.",
                    unique,
                ),
            }
        )
        pending_media.clear()

    for message in history:
        if is_internal_visual_context(message.get("content")):
            continue
        role = message.get("role")
        if role not in {"system", "user", "assistant", "tool"}:
            continue
        if role != "tool":
            flush_tool_media()

        allowed = {
            "system": ("role", "content", "name"),
            "user": ("role", "content", "name"),
            "assistant": ("role", "content", "name", "tool_calls"),
            "tool": ("role", "content", "tool_call_id"),
        }[role]
        wire_message = {key: message[key] for key in allowed if key in message}
        wire_message.setdefault("content", "")

        model_content = message.get("model_content") or []
        if role == "user" and model_content:
            wire_message["content"] = user_content(
                str(message.get("content", "")),
                [dict(item) for item in model_content],
            )
        projected.append(wire_message)

        if role == "tool" and model_content:
            source = str(message.get("tool_name") or "tool")
            pending_media.extend((source, dict(item)) for item in model_content)

    flush_tool_media()
    return projected


def serialize_anthropic_messages(
    system_prompt: str,
    history: list[dict],
) -> list[dict]:
    """Translate canonical history into Anthropic Messages wire messages.

    System content remains represented as a leading pseudo-message here. The
    Anthropic provider extracts it into the top-level ``system`` request field.
    """
    system_parts = [system_prompt]
    projected: list[dict] = []

    for message in history:
        if is_internal_visual_context(message.get("content")):
            continue
        role = message.get("role")
        if role == "system":
            text = str(message.get("content") or "").strip()
            if text:
                system_parts.append(text)
            continue
        if role == "user":
            blocks = _anthropic_user_blocks(message)
            if blocks:
                _append_anthropic_message(projected, "user", blocks)
            continue
        if role == "assistant":
            blocks = _anthropic_assistant_blocks(message)
            if blocks:
                _append_anthropic_message(projected, "assistant", blocks)
            continue
        if role == "tool":
            block = _anthropic_tool_result(message)
            _append_anthropic_message(projected, "user", [block])

    return [
        {"role": "system", "content": "\n\n".join(part for part in system_parts if part)},
        *projected,
    ]


def model_content_identity(item: dict) -> tuple[str, str]:
    part_type = str(item.get("type", ""))
    image = item.get("image_url")
    if isinstance(image, dict):
        return part_type, str(image.get("url", ""))
    return part_type, json.dumps(item, ensure_ascii=False, sort_keys=True)
def _append_anthropic_message(messages: list[dict], role: str, blocks: list[dict]) -> None:
    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].extend(blocks)
        return
    messages.append({"role": role, "content": list(blocks)})


def _anthropic_user_blocks(message: dict) -> list[dict]:
    blocks: list[dict] = []
    content = message.get("content", "")
    if isinstance(content, str):
        if content:
            blocks.append({"type": "text", "text": content})
    elif isinstance(content, list):
        for part in content:
            converted = _anthropic_input_part(part)
            if converted is not None:
                blocks.append(converted)
    elif content:
        blocks.append({"type": "text", "text": str(content)})

    for part in message.get("model_content") or []:
        converted = _anthropic_input_part(part)
        if converted is not None and converted not in blocks:
            blocks.append(converted)
    return blocks


def _anthropic_assistant_blocks(message: dict) -> list[dict]:
    native_blocks = message.get("model_content") or []
    if native_blocks:
        return [dict(block) for block in native_blocks]

    blocks: list[dict] = []
    content = message.get("content")
    if isinstance(content, str) and content:
        blocks.append({"type": "text", "text": content})
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                blocks.append({"type": "text", "text": str(part["text"])})

    for tool_call in message.get("tool_calls") or []:
        function = tool_call.get("function") or {}
        raw_arguments = function.get("arguments", "{}")
        if isinstance(raw_arguments, dict):
            arguments = dict(raw_arguments)
        else:
            try:
                parsed = json.loads(str(raw_arguments or "{}"))
                arguments = parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                arguments = {}
        blocks.append(
            {
                "type": "tool_use",
                "id": str(tool_call.get("id") or ""),
                "name": str(function.get("name") or ""),
                "input": arguments,
            }
        )
    return blocks


def _anthropic_tool_result(message: dict) -> dict:
    content: list[dict] = []
    text = message.get("content")
    if text:
        content.append({"type": "text", "text": str(text)})
    for part in message.get("model_content") or []:
        converted = _anthropic_input_part(part)
        if converted is not None:
            content.append(converted)
    return {
        "type": "tool_result",
        "tool_use_id": str(message.get("tool_call_id") or ""),
        # Anthropic permits an empty string here, while an empty text block is
        # rejected by the Messages API.
        "content": content or "",
    }


def _anthropic_input_part(part: Any) -> dict | None:
    if not isinstance(part, dict):
        return None
    part_type = part.get("type")
    if part_type in {"text", "input_text"}:
        text = part.get("text")
        return {"type": "text", "text": str(text)} if text is not None else None
    if part_type not in {"image_url", "input_image"}:
        return None

    image = part.get("image_url")
    url = image.get("url") if isinstance(image, dict) else image
    if not isinstance(url, str) or not url:
        return None
    match = _DATA_URL.match(url)
    if match:
        # Validate once at the projection boundary so malformed checkpoints do
        # not become opaque provider-side 400 responses.
        base64.b64decode(match.group("data"), validate=True)
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": match.group("media_type"),
                "data": match.group("data"),
            },
        }
    return {"type": "image", "source": {"type": "url", "url": url}}
