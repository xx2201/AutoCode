"""Tool execution policy."""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..state import PolicyDecision

_PROTECTED_NAMES = {".env"}
_PROTECTED_PARTS = {".git"}
_READ_ONLY_COMMANDS = (
    "pwd",
    "ls",
    "dir",
    "rg ",
    "git status",
    "git diff",
    "pytest",
    "python -m pytest",
    "Get-ChildItem",
    "Get-Content",
    "type ",
    "cat ",
)
_DENY_COMMAND_PATTERNS = [
    (r":\(\)\s*\{.*:\|:.*\}", "fork bomb"),
    (r"\bcurl\b.*\|\s*(sudo\s+)?bash", "pipe curl to bash"),
]
_MANUAL_CONFIRM_COMMAND_PATTERNS = [
    (r"\brm\s+(-\w*)?-rf\s", "force recursive delete requires explicit confirmation"),
    (r"\bdel\s+/[qsf]", "destructive delete requires explicit confirmation"),
    (r"\bRemove-Item\b.*-Recurse", "recursive remove requires explicit confirmation"),
    (r"\bgit\s+reset\s+--hard\b", "destructive git reset requires explicit confirmation"),
]
_CONFIRM_COMMAND_PATTERNS = [
    (r"\bgit\s+clean\b", "git clean modifies the workspace"),
    (r"\bpip\s+install\b", "package installation changes the environment"),
    (r">\s*[^|]+", "shell redirection writes files"),
    (r"\bMove-Item\b", "move changes files"),
    (r"\bCopy-Item\b", "copy changes files"),
]


class Policy:
    def __init__(self, workspace_root: str | None = None, auto_approve: bool = False):
        self.workspace_root = Path(workspace_root or os.getcwd()).expanduser().resolve()
        self.auto_approve = auto_approve

    def evaluate_tool_call(self, tool_name: str, arguments: dict) -> PolicyDecision:
        if tool_name == "bash":
            return self._evaluate_bash(arguments.get("command", ""))
        if tool_name in {"read_file", "write_file", "edit_file"}:
            return self._evaluate_path(arguments.get("file_path"))
        if tool_name in {"grep", "glob"}:
            path = arguments.get("path")
            if path is None:
                return PolicyDecision("allow")
            return self._evaluate_path(path, allow_protected=False)
        if tool_name == "todo_write":
            return PolicyDecision("allow")
        if tool_name == "agent":
            return PolicyDecision("confirm", "sub-agent execution should be explicitly approved")
        return PolicyDecision("allow")

    def _evaluate_path(self, raw_path: str | None, allow_protected: bool = False) -> PolicyDecision:
        if not raw_path:
            return PolicyDecision("deny", "missing file path")

        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = (self.workspace_root / path).resolve()
        else:
            path = path.resolve()

        try:
            path.relative_to(self.workspace_root)
        except ValueError:
            return PolicyDecision("deny", f"path must stay inside workspace: {self.workspace_root}")

        if not allow_protected:
            if path.name in _PROTECTED_NAMES:
                return PolicyDecision("deny", f"{path.name} is protected")
            if any(part in _PROTECTED_PARTS for part in path.parts):
                return PolicyDecision("deny", "protected directories cannot be modified")

        return PolicyDecision("allow")

    def _evaluate_bash(self, command: str) -> PolicyDecision:
        command = command.strip()
        if not command:
            return PolicyDecision("deny", "empty command")

        for pattern, reason in _DENY_COMMAND_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return PolicyDecision("deny", reason)

        for pattern, reason in _MANUAL_CONFIRM_COMMAND_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return PolicyDecision("confirm", reason, requires_manual=True)

        for prefix in _READ_ONLY_COMMANDS:
            if command.startswith(prefix):
                return PolicyDecision("allow")

        for pattern, reason in _CONFIRM_COMMAND_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return PolicyDecision("allow" if self.auto_approve else "confirm", reason)

        return PolicyDecision(
            "allow" if self.auto_approve else "confirm",
            "command is not in the default read-only allowlist",
        )
