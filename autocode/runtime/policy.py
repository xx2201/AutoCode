"""Tool execution policy."""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..state import PolicyDecision

_PROTECTED_NAMES = {".env"}
_PROTECTED_PARTS = {".git"}
_SHELL_DELETE_PREFIX = r"(?:^|[;&|]|\n)\s*"
_DENY_COMMAND_PATTERNS = [
    (r":\(\)\s*\{.*:\|:.*\}", "fork bomb"),
    (r"\bcurl\b.*\|\s*(sudo\s+)?bash", "pipe curl to bash"),
    (r"\bgit\s+reset\s+--hard\b", "destructive git reset modifies the workspace"),
    (r"\bgit\s+clean\b", "git clean modifies the workspace"),
    (
        r"\b(taskkill|Stop-Process|killall|pkill)\b|\bkill\s+-\d+\b|\bkill\s+\d+\b",
        "process termination via shell is not allowed; use stop_process for managed background processes",
    ),
]
_STREAMING_COMMAND_PATTERNS = [
    r"\bredis-cli\b[^\n]*\bMONITOR\b",
    r"\btail\b[^\n]*\s-f\b",
    r"\bwatch\b\b",
]


class Policy:
    def __init__(self, workspace_root: str | None = None, auto_approve: bool = False):
        self.workspace_root = Path(workspace_root or os.getcwd()).expanduser().resolve()
        self.auto_approve = auto_approve

    def evaluate_tool_call(self, tool_name: str, arguments: dict) -> PolicyDecision:
        if tool_name.startswith("mcp_"):
            return PolicyDecision("confirm", "external MCP tool call", requires_manual=False)
        if tool_name == "bash":
            return self._evaluate_bash(arguments.get("command", ""))
        if tool_name in {"read_file", "write_file", "edit_file"}:
            return self._evaluate_path(arguments.get("file_path"))
        if tool_name == "delete_path":
            decision = self._evaluate_path(arguments.get("path"))
            if decision.action == "deny":
                return decision
            return PolicyDecision(
                "confirm",
                "deleting files modifies the workspace",
                requires_manual=True,
            )
        if tool_name in {"grep", "glob"}:
            path = arguments.get("path")
            if path is None:
                return PolicyDecision("allow")
            return self._evaluate_path(path, allow_protected=False)
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

        dangerous = self._evaluate_workspace_scoped_dangerous_command(command)
        if dangerous is not None:
            return dangerous

        streaming = self._evaluate_streaming_command(command)
        if streaming is not None:
            return streaming

        redirect_decision = self._evaluate_redirect_targets(command)
        if redirect_decision is not None:
            return redirect_decision

        return PolicyDecision("allow")

    def _evaluate_redirect_targets(self, command: str) -> PolicyDecision | None:
        for match in re.finditer(r"(?:^|\s)(?:\d?>>?|>>?)\s*(?P<path>[^\s&|;]+)", command):
            target = match.group("path").strip().strip("'\"")
            if not target or target.startswith("&"):
                continue
            decision = self._evaluate_path(target)
            if decision.action == "deny":
                return decision
        return None

    def _evaluate_workspace_scoped_dangerous_command(self, command: str) -> PolicyDecision | None:
        scan = _mask_quoted_content(command)
        if not re.search(
            _SHELL_DELETE_PREFIX + r"(?:rm|rmdir|rd|del|erase|Remove-Item)\b",
            scan,
            re.IGNORECASE,
        ):
            return None
        return PolicyDecision("deny", "delete via shell is not allowed; use delete_path instead")

    def _evaluate_streaming_command(self, command: str) -> PolicyDecision | None:
        scan = _mask_quoted_content(command)
        for pattern in _STREAMING_COMMAND_PATTERNS:
            if re.search(pattern, scan, re.IGNORECASE):
                return PolicyDecision(
                    "deny",
                    "streaming or long-running shell command is not allowed; use start_process instead",
                )
        return None


def _mask_quoted_content(command: str) -> str:
    chars: list[str] = []
    quote: str | None = None
    escape = False
    for ch in command:
        if quote is None:
            if ch in {"'", '"'}:
                quote = ch
            chars.append(ch)
            continue

        if escape:
            escape = False
            chars.append("\n" if ch == "\n" else " ")
            continue

        if quote == '"' and ch == "\\":
            escape = True
            chars.append(" ")
            continue

        if ch == quote:
            quote = None
            chars.append(ch)
            continue

        chars.append("\n" if ch == "\n" else " ")
    return "".join(chars)
