"""Background process tools for long-running commands."""

from .base import ConcurrencySpec, Tool
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
        "Use this instead of bash for servers, workers, consumers, or watchers. "
        "Temporary test processes are auto-cleaned by the runtime when the task ends."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Command to start in the background"},
            "cwd": {"type": "string", "description": "Working directory inside the workspace"},
            "log_file": {"type": "string", "description": "Optional log file path inside the workspace"},
            "keep_alive": {
                "type": "boolean",
                "description": (
                    "Set true only when the user explicitly wants the process to stay running "
                    "after the current task completes. Managed processes are still cleaned when "
                    "the agent resets or exits."
                ),
            },
        },
        "required": ["command"],
    }

    def concurrency_spec(self, arguments: dict) -> ConcurrencySpec:
        return ConcurrencySpec.resources(
            writes={"process-manager"},
            reason="process creation allocates a new managed identifier",
        )

    def execute(
        self,
        command: str,
        cwd: str = ".",
        log_file: str | None = None,
        keep_alive: bool = False,
        _task_id: str | None = None,
    ) -> str:
        try:
            return _manager(self).start_process(
                command=command,
                cwd=cwd,
                log_file=log_file,
                keep_alive=keep_alive,
                task_id=_task_id,
            )
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

    def concurrency_spec(self, arguments: dict) -> ConcurrencySpec:
        process_id = str(arguments["process_id"])
        return ConcurrencySpec.resources(
            reads={f"process:{process_id}"},
            reason="different managed processes are independent",
        )

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

    def concurrency_spec(self, arguments: dict) -> ConcurrencySpec:
        process_id = str(arguments["process_id"])
        return ConcurrencySpec.resources(
            reads={f"process:{process_id}"},
            reason="different managed processes are independent",
        )

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

    def concurrency_spec(self, arguments: dict) -> ConcurrencySpec:
        process_id = str(arguments["process_id"])
        return ConcurrencySpec.resources(
            writes={f"process:{process_id}"},
            reason="stopping conflicts with reads and waits for the same process",
        )

    def execute(self, process_id: str) -> str:
        try:
            return _manager(self).stop_process(process_id=process_id)
        except Exception as e:
            return f"Error: {e}"
