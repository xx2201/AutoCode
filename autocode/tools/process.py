"""Background process tools for long-running commands."""

from .base import Tool
from ..infra.processes import BackgroundProcessManager


def _manager(tool: Tool) -> BackgroundProcessManager:
    mgr = getattr(tool, "_process_manager", None)
    if mgr is None:
        fs = getattr(tool, "_fs", None)
        root = str(fs.workspace_root) if fs else "."
        mgr = BackgroundProcessManager(root)
        setattr(tool, "_process_manager", mgr)
    return mgr


class StartProcessTool(Tool):
    name = "start_process"
    description = (
        "Start a long-running background process and return a managed process id. "
        "Use this instead of bash for servers, workers, consumers, or watchers."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Command to start in the background"},
            "cwd": {"type": "string", "description": "Working directory inside the workspace"},
            "log_file": {"type": "string", "description": "Optional log file path inside the workspace"},
        },
        "required": ["command"],
    }

    def execute(self, command: str, cwd: str = ".", log_file: str | None = None) -> str:
        try:
            return _manager(self).start_process(command=command, cwd=cwd, log_file=log_file)
        except Exception as e:
            return f"Error: {e}"


class ReadProcessOutputTool(Tool):
    name = "read_process_output"
    description = "Read the latest output from a managed background process."
    parameters = {
        "type": "object",
        "properties": {
            "process_id": {"type": "string", "description": "Managed background process id"},
            "tail_lines": {"type": "integer", "description": "Number of recent log lines to return"},
        },
        "required": ["process_id"],
    }

    def execute(self, process_id: str, tail_lines: int = 50) -> str:
        try:
            return _manager(self).read_output(process_id=process_id, tail_lines=tail_lines)
        except Exception as e:
            return f"Error: {e}"


class WaitForProcessOutputTool(Tool):
    name = "wait_for_process_output"
    description = "Wait until a managed process emits output matching a regex pattern."
    parameters = {
        "type": "object",
        "properties": {
            "process_id": {"type": "string", "description": "Managed background process id"},
            "pattern": {"type": "string", "description": "Regex pattern to wait for"},
            "timeout": {"type": "integer", "description": "Timeout in seconds"},
        },
        "required": ["process_id", "pattern"],
    }

    def execute(self, process_id: str, pattern: str, timeout: int = 30) -> str:
        try:
            return _manager(self).wait_for_output(process_id=process_id, pattern=pattern, timeout=timeout)
        except Exception as e:
            return f"Error: {e}"


class StopProcessTool(Tool):
    name = "stop_process"
    description = "Stop a managed background process."
    parameters = {
        "type": "object",
        "properties": {
            "process_id": {"type": "string", "description": "Managed background process id"},
        },
        "required": ["process_id"],
    }

    def execute(self, process_id: str) -> str:
        try:
            return _manager(self).stop_process(process_id=process_id)
        except Exception as e:
            return f"Error: {e}"
