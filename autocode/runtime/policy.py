"""Tool execution policy."""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..state import PolicyDecision

_PROTECTED_NAMES = {".env"}
_PROTECTED_PARTS = {".git"}
_DENY_COMMAND_PATTERNS = [
    (r":\(\)\s*\{.*:\|:.*\}", "fork bomb"),
    (r"\bcurl\b.*\|\s*(sudo\s+)?bash", "pipe curl to bash"),
    (r"\bgit\s+reset\s+--hard\b", "destructive git reset modifies the workspace"),
    (r"\bgit\s+clean\b", "git clean modifies the workspace"),
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
        target = None
        reason = ""

        rm_match = re.search(r"\brm\s+(-\w+\s+)*(-rf|-fr)\s+(?P<path>[^\s&|;]+)", command, re.IGNORECASE)
        if rm_match:
            target = rm_match.group("path")
            reason = "force recursive delete modifies the workspace"
        else:
            del_match = re.search(r"\bdel\b(?:\s+/[^\s]+)*\s+(?P<path>[^\s&|;]+)", command, re.IGNORECASE)
            if del_match:
                target = del_match.group("path")
                reason = "delete modifies the workspace"
            else:
                remove_match = re.search(
                    r"\bRemove-Item\b(?:\s+-[^\s]+\s+[^\s]+\s+)*\s+(?P<path>[^\s&|;]+)",
                    command,
                    re.IGNORECASE,
                )
                if remove_match and re.search(r"\b-Recurse\b", command, re.IGNORECASE):
                    target = remove_match.group("path")
                    reason = "recursive remove modifies the workspace"

        if not reason:
            return None
        if not target:
            return PolicyDecision("deny", reason)

        path_decision = self._evaluate_path(target, allow_protected=True)
        if path_decision.action == "deny":
            return PolicyDecision("deny", f"dangerous command target must stay inside workspace: {target}")
        return PolicyDecision("deny", reason)
