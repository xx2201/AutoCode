"""Workspace image loading for multimodal model turns."""

from __future__ import annotations

import base64
import mimetypes

from .base import Tool, ToolResult


_SUPPORTED_IMAGE_TYPES = {
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}
_MAX_IMAGE_BYTES = 10 * 1024 * 1024


class ReadImageTool(Tool):
    name = "read_image"
    description = (
        "Load an image from the current workspace so the multimodal model can inspect it. "
        "Use this for PNG, JPEG, WEBP, or non-animated GIF files instead of read_file."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to an image inside the current workspace",
            },
            "detail": {
                "type": "string",
                "enum": ["auto", "low", "high"],
                "description": "Vision detail level. Default auto.",
            },
        },
        "required": ["file_path"],
    }

    def execute(self, file_path: str, detail: str = "auto") -> ToolResult | str:
        try:
            fs = getattr(self, "_fs", None)
            if fs is None:
                return "Error: read_image requires a workspace filesystem"
            path = fs.resolve_path(file_path)
            if not path.exists():
                return f"Error: {file_path} not found"
            if not path.is_file():
                return f"Error: {file_path} is a directory, not an image"

            media_type = mimetypes.guess_type(path.name)[0] or ""
            if media_type not in _SUPPORTED_IMAGE_TYPES:
                return (
                    "Error: unsupported image type. "
                    "Use PNG, JPEG, WEBP, or a non-animated GIF."
                )
            size = path.stat().st_size
            if size > _MAX_IMAGE_BYTES:
                return f"Error: image exceeds the {_MAX_IMAGE_BYTES // (1024 * 1024)} MB limit"

            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            data_url = f"data:{media_type};base64,{encoded}"
            relative_path = path.relative_to(fs.workspace_root).as_posix()
            return ToolResult(
                text=f"Loaded image for visual inspection: {relative_path} ({size} bytes)",
                model_content=[
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url, "detail": detail},
                    }
                ],
            )
        except Exception as exc:
            return f"Error: {exc}"
