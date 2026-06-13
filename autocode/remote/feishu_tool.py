"""Feishu-only tool for sending local attachments back to the current chat."""

from __future__ import annotations

from ..tools.base import Tool


class FeishuSendTool(Tool):
    name = "feishu_send"
    description = (
        "Send a local file from the current workspace back to the current Feishu chat. "
        "Use this when the user asks you to send a generated screenshot, image, PDF, or other file."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to a local file inside the current workspace.",
            },
        },
        "required": ["file_path"],
    }

    def __init__(self, sender=None):
        self._sender = sender

    def clone(self) -> "FeishuSendTool":
        return type(self)(self._sender)

    def execute(self, file_path: str) -> str:
        if self._sender is None:
            return "Error: feishu_send tool is not initialized."
        try:
            return self._sender(file_path)
        except Exception as exc:
            return f"Error sending Feishu attachment: {exc}"
