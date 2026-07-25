"""Validate and persist browser attachments inside the selected local workspace."""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path


MAX_ATTACHMENT_COUNT = 5
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 25 * 1024 * 1024
SUPPORTED_VISION_TYPES = {
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+")


@dataclass
class PreparedAttachments:
    prompt: str
    image_parts: list[dict] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)


def prepare_attachments(
    workspace_root: str | Path,
    client_id: str,
    prompt: str,
    attachments: list[dict] | None,
) -> PreparedAttachments:
    items = list(attachments or [])
    if len(items) > MAX_ATTACHMENT_COUNT:
        raise ValueError(f"At most {MAX_ATTACHMENT_COUNT} attachments are allowed.")
    if not items:
        return PreparedAttachments(prompt=prompt)

    workspace = Path(workspace_root).expanduser().resolve()
    upload_root = workspace / ".autocode" / "uploads"
    _ensure_private_autocode_dir(upload_root.parent)
    batch_dir = upload_root / _safe_segment(client_id) / uuid.uuid4().hex
    batch_dir.mkdir(parents=True, exist_ok=False)

    total_size = 0
    image_parts: list[dict] = []
    files: list[dict] = []
    for index, item in enumerate(items, start=1):
        name = _safe_filename(str(item.get("name") or f"attachment-{index}"))
        media_type = str(item.get("media_type") or "application/octet-stream").lower()
        encoded = str(item.get("data_base64") or "")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError(f"Attachment '{name}' is not valid base64 data.") from exc
        if not content:
            raise ValueError(f"Attachment '{name}' is empty.")
        if len(content) > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"Attachment '{name}' exceeds the 10 MB limit.")
        total_size += len(content)
        if total_size > MAX_TOTAL_ATTACHMENT_BYTES:
            raise ValueError("Attachments exceed the 25 MB total limit.")

        target = batch_dir / name
        if target.exists():
            target = batch_dir / f"{target.stem}-{index}{target.suffix}"
        target.write_bytes(content)
        relative = target.relative_to(workspace).as_posix()
        file_info = {
            "name": name,
            "path": relative,
            "media_type": media_type,
            "size": len(content),
            "is_image": media_type in SUPPORTED_VISION_TYPES,
        }
        files.append(file_info)

        if media_type.startswith("image/") and media_type not in SUPPORTED_VISION_TYPES:
            raise ValueError(
                f"Image '{name}' is not supported. Use PNG, JPEG, WEBP, or non-animated GIF."
            )
        if media_type in SUPPORTED_VISION_TYPES:
            image_parts.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{media_type};base64,{encoded}",
                        "detail": "auto",
                    },
                }
            )

    file_lines = "\n".join(
        f"- {item['name']} ({item['media_type']}, {item['size']} bytes): {item['path']}"
        for item in files
    )
    instruction = (
        "\n\n[Uploaded files are available in the local workspace]\n"
        f"{file_lines}\n"
        "Use read for text, code, image, PDF, or notebook files when needed."
    )
    effective_prompt = (prompt.strip() or "请分析我上传的文件。") + instruction
    return PreparedAttachments(
        prompt=effective_prompt,
        image_parts=image_parts,
        files=files,
    )


def _safe_filename(name: str) -> str:
    leaf = Path(name).name.strip().replace("\x00", "")
    cleaned = _SAFE_NAME_RE.sub("_", leaf).strip("._")
    return (cleaned or "attachment")[:180]


def _safe_segment(value: str) -> str:
    return _SAFE_NAME_RE.sub("_", value).strip("._")[:80] or "web"


def _ensure_private_autocode_dir(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    ignore_file = directory / ".gitignore"
    if not ignore_file.exists():
        ignore_file.write_text("*\n", encoding="utf-8")
