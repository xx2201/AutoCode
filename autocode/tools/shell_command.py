"""Explicit cross-platform shell command tool."""

from __future__ import annotations

import json

from ..infra.shell import available_shell_names, default_shell_name
from .base import ConcurrencySpec, Tool, ToolResult

class ShellCommandTool(Tool):
    name = "shell_command"

    def __init__(self) -> None:
        default = default_shell_name()
        shells = available_shell_names()
        self.description = (
            f"Run a short-lived command with an explicit shell. The default on this "
            f"platform is {default}. Returns structured JSON containing exit_code, "
            "stdout, stderr, timeout and truncation state, plus a full output path when needed. "
            "Use workdir instead of writing cd into the command. Use start_process for servers, "
            "workers, consumers, or watchers. Use delete_path for deletion."
        )
        self.parameters = {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": f"Command written for the selected shell ({', '.join(shells)})",
                },
                "workdir": {
                    "type": "string",
                    "description": "Working directory inside the workspace (default '.')",
                },
                "shell": {
                    "type": "string",
                    "enum": list(shells),
                    "description": f"Interpreter to use (default '{default}')",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 120)",
                },
            },
            "required": ["command"],
        }

    def concurrency_spec(self, arguments: dict) -> ConcurrencySpec:
        return ConcurrencySpec.exclusive(
            "arbitrary shell commands may mutate files, processes, dependencies, or Git state"
        )

    def execute(
        self,
        command: str,
        workdir: str = ".",
        shell: str | None = None,
        timeout: int = 120,
    ) -> ToolResult:
        sandbox = getattr(self, "_sandbox", None)
        if sandbox is None:
            return ToolResult(
                text=json.dumps({
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": "shell_command requires an attached sandbox provider",
                    "timed_out": False,
                    "truncated": False,
                    "full_output_path": None,
                    "cwd": str(workdir),
                    "shell": shell or default_shell_name(),
                }, ensure_ascii=False, indent=2),
                is_error=True,
            )
        try:
            result = sandbox.run(
                command=command,
                timeout=timeout,
                workdir=workdir,
                shell=shell,
            )
        except Exception as exc:
            payload = {
                "exit_code": -1,
                "stdout": "",
                "stderr": str(exc),
                "timed_out": False,
                "truncated": False,
                "full_output_path": None,
                "cwd": str(workdir),
                "shell": shell or default_shell_name(),
            }
            return ToolResult(text=json.dumps(payload, ensure_ascii=False, indent=2), is_error=True)
        return ToolResult(
            text=json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            is_error=result.exit_code != 0 or result.timed_out,
        )
