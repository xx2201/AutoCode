import threading
import time

from autocode.context.prompt import static_system_prompt
from autocode.llm import ToolCall
from autocode.runtime import HookBus, Runtime
from autocode.runtime.scheduler import plan_execution_groups
from autocode.state import TurnState
from autocode.tools.base import ConcurrencySpec, Tool
from autocode.tools.shell_command import ShellCommandTool
from autocode.tools.process import ReadProcessOutputTool, StopProcessTool


class _ResourceTool(Tool):
    name = "resource"
    description = "Concurrency test tool"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, *, delays=None):
        self.delays = delays or {}
        self.active = 0
        self.max_active = 0
        self.execute_threads = []
        self._lock = threading.Lock()

    def concurrency_spec(self, arguments: dict) -> ConcurrencySpec:
        resource = str(arguments["resource"])
        if arguments.get("exclusive"):
            return ConcurrencySpec.exclusive("test barrier")
        if arguments.get("read"):
            return ConcurrencySpec.resources(reads={resource})
        return ConcurrencySpec.resources(writes={resource})

    def execute(self, resource: str, read: bool = False, exclusive: bool = False) -> str:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.execute_threads.append(threading.get_ident())
        time.sleep(self.delays.get(resource, 0.05))
        with self._lock:
            self.active -= 1
        return resource


class _RecordingRecovery:
    def __init__(self):
        self.thread_ids = []

    def note_tool_result(self, turn_state, tool_name, result):
        self.thread_ids.append(threading.get_ident())
        return result


def _call(call_id: str, resource: str, **extra) -> ToolCall:
    return ToolCall(
        id=call_id,
        name="resource",
        arguments={"resource": resource, **extra},
    )


def _state() -> TurnState:
    return TurnState(turn_id="turn_concurrency", status="running")


def test_scheduler_groups_independent_resources_and_separates_conflicts():
    specs = [
        ConcurrencySpec.resources(reads={"file:a"}),
        ConcurrencySpec.resources(writes={"file:b"}),
        ConcurrencySpec.resources(writes={"file:a"}),
        ConcurrencySpec.exclusive("git mutation"),
        ConcurrencySpec.parallel(),
    ]

    groups = plan_execution_groups(specs)

    assert [group.call_indexes for group in groups] == [
        (0, 1),
        (2,),
        (3,),
        (4,),
    ]


def test_runtime_overlaps_independent_resources_and_preserves_result_order():
    tool = _ResourceTool(delays={"slow": 0.08, "fast": 0.01})
    runtime = Runtime({"resource": tool})

    results = runtime.execute_tool_calls_parallel(
        _state(),
        [_call("1", "slow"), _call("2", "fast")],
        "session_concurrency",
    )

    assert results == ["slow", "fast"]
    assert tool.max_active == 2


def test_runtime_serializes_calls_that_write_the_same_resource():
    tool = _ResourceTool()
    runtime = Runtime({"resource": tool})

    results = runtime.execute_tool_calls_parallel(
        _state(),
        [_call("1", "same"), _call("2", "same")],
        "session_concurrency",
    )

    assert results == ["same", "same"]
    assert tool.max_active == 1


def test_runtime_finalizes_hooks_and_recovery_on_the_calling_thread():
    tool = _ResourceTool()
    recovery = _RecordingRecovery()
    hooks = HookBus()
    hook_threads = []
    hooks.on("before_tool", lambda *_: hook_threads.append(threading.get_ident()))
    hooks.on("after_tool", lambda *_: hook_threads.append(threading.get_ident()))
    runtime = Runtime({"resource": tool}, hooks=hooks, recovery=recovery)
    caller_thread = threading.get_ident()

    runtime.execute_tool_calls_parallel(
        _state(),
        [_call("1", "a"), _call("2", "b")],
        "session_concurrency",
    )

    assert set(hook_threads) == {caller_thread}
    assert set(recovery.thread_ids) == {caller_thread}
    assert tool.execute_threads
    assert caller_thread not in tool.execute_threads


def test_builtin_parameter_level_specs_distinguish_safe_and_global_operations(tmp_path):
    shell = ShellCommandTool()
    shell._fs = type("_FS", (), {"workspace_root": tmp_path})()

    for command in (
        "pytest -q",
        "git status --short",
        "git commit -m test",
        'git -C "C:/workspace" push',
        "cd api && pytest -q",
        "protoc --go_out=. api.proto",
    ):
        assert shell.concurrency_spec({"command": command}).mode.value == "exclusive"

    read_process = ReadProcessOutputTool()
    stop_process = StopProcessTool()
    read_spec = read_process.concurrency_spec({"process_id": "p1"})
    stop_same = stop_process.concurrency_spec({"process_id": "p1"})
    stop_other = stop_process.concurrency_spec({"process_id": "p2"})
    assert plan_execution_groups([read_spec, stop_same])[1].call_indexes == (1,)
    assert plan_execution_groups([read_spec, stop_other])[0].call_indexes == (0, 1)


def test_system_prompt_requests_independent_batches_and_rejects_blind_retries():
    prompt = static_system_prompt([], cwd="C:/workspace")

    assert "Batch independent tool calls" in prompt
    assert "Keep dependencies sequential" in prompt
    assert "Do not repeat successful calls blindly" in prompt
    assert "normally two to four complete sentences" in prompt
    assert "user-facing summaries of the work" in prompt
    assert "follow the investigation without opening every tool result" in prompt
    assert "what was learned, why it matters, and what will happen next" in prompt
    assert "Treat that block as metadata" in prompt
    assert "继续执行" not in prompt
