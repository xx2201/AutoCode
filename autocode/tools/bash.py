"""Shell command execution via a lightweight sandbox."""

import os
import re

from .base import ConcurrencySpec, Tool
from ..infra import Sandbox

_DANGEROUS_PATTERNS = [
    (r":\(\)\s*\{.*:\|:.*\}", "fork bomb"),
    (r"\bcurl\b.*\|\s*(sudo\s+)?bash", "pipe curl to bash"),
]

_EXCLUSIVE_COMMAND_PATTERNS = (
    (r"(^|[;&|]\s*)cd(?:\s|$)", "changes the shared shell working directory"),
    (
        r"\bgit(?:\.exe)?(?:\s+-C\s+(?:\"[^\"]+\"|'[^']+'|\S+))*\s+"
        r"(?:add|am|apply|branch|checkout|cherry-pick|clean|clone|commit|fetch|"
        r"init|merge|mv|pull|push|rebase|remote|reset|restore|revert|rm|stash|"
        r"submodule|switch|tag|worktree)\b",
        "mutates shared Git state",
    ),
    (
        r"\b(?:go\s+generate|wire|protoc|buf\s+generate|sqlc\s+generate)\b",
        "generates files across the workspace",
    ),
    (
        r"\b(?:alembic\s+upgrade|prisma\s+migrate|migrate\s+(?:up|down)|"
        r"django-admin\s+migrate|manage\.py\s+migrate)\b",
        "mutates shared database or migration state",
    ),
    (
        r"\b(?:npm|pnpm|yarn)\s+(?:add|install|remove|uninstall|update|upgrade)\b|"
        r"\bpip(?:3)?\s+(?:install|uninstall)\b|"
        r"\bgo\s+(?:get|mod\s+(?:tidy|vendor))\b",
        "mutates dependency and lock files",
    ),
)


class BashTool(Tool):
    name = "bash"
    description = (
        "Execute a shell command. Returns stdout, stderr, and exit code. "
        "Use this for short-lived commands like tests, git operations, and quick checks. "
        "Use start_process for servers, consumers, workers, or other long-running commands. "
        "Use delete_path for deleting workspace files or directories."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to run",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default 120)",
            },
        },
        "required": ["command"],
    }

    def concurrency_spec(self, arguments: dict) -> ConcurrencySpec:
        command = str(arguments.get("command") or "")
        for pattern, reason in _EXCLUSIVE_COMMAND_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return ConcurrencySpec.exclusive(reason)
        return ConcurrencySpec.parallel("ordinary shell commands run in independent processes")

    def execute(self, command: str, timeout: int = 120, _confirmed_sensitive: bool = False) -> str:
        warning = None if _confirmed_sensitive else _check_dangerous(command)
        if warning:
            return f"⚠ Blocked: {warning}\nCommand: {command}\nIf intentional, modify the command to be more specific."
        sandbox = getattr(self, "_sandbox", None) or Sandbox(os.getcwd())
        try:
            result = sandbox.run(command=command, timeout=timeout)
            if result.exit_code != 0:
                return f"Error: command exited with code {result.exit_code}\n{result.output}"
            return result.output
        except TimeoutError:
            return f"Error: timed out after {timeout}s"
        except Exception as e:
            return f"Error running command: {e}"


def _check_dangerous(command: str) -> str | None:
    for pattern, reason in _DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return reason
    return None
