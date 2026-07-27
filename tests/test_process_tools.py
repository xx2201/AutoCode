import subprocess
import sys
import time
from pathlib import Path

import pytest

from autocode.infra import BackgroundProcessManager, WorkspaceFS
from autocode.infra import processes as process_module
from autocode.runtime import Policy
from autocode.tools.process import (
    ReadProcessOutputTool,
    StartProcessTool,
    StopProcessTool,
    WaitForProcessOutputTool,
)


def _wire(tool, tmp_path, manager):
    setattr(tool, "_fs", WorkspaceFS(str(tmp_path)))
    setattr(tool, "_process_manager", manager)
    return tool


def test_background_process_tools_round_trip(tmp_path):
    manager = BackgroundProcessManager(str(tmp_path))
    start = _wire(StartProcessTool(), tmp_path, manager)
    read = _wire(ReadProcessOutputTool(), tmp_path, manager)
    wait = _wire(WaitForProcessOutputTool(), tmp_path, manager)
    stop = _wire(StopProcessTool(), tmp_path, manager)

    command = (
        f'"{sys.executable}" -c "import sys,time; '
        "print('ready'); sys.stdout.flush(); time.sleep(30)\""
    )

    started = start.execute(command=command, cwd=".")
    process_id = started.splitlines()[0].split()[-1]

    waited = wait.execute(process_id=process_id, pattern="ready", timeout=10)
    assert "Matched pattern" in waited

    output = read.execute(process_id=process_id, tail_lines=20)
    assert "ready" in output
    assert "Status: running" in output

    stopped = stop.execute(process_id=process_id)
    assert process_id in stopped


def test_background_process_preserves_utf8_output(tmp_path):
    manager = BackgroundProcessManager(str(tmp_path))
    command = (
        f'"{sys.executable}" -c "import sys,time; '
        "print('预计耗时 2 秒'); sys.stdout.flush(); time.sleep(30)\""
    )

    started = manager.start_process(command=command, cwd=".")
    process_id = started.splitlines()[0].split()[-1]
    try:
        waited = manager.wait_for_output(process_id, pattern="预计耗时 2 秒", timeout=10)
        assert "预计耗时 2 秒" in waited
    finally:
        manager.stop_process(process_id)


def test_read_output_decodes_non_utf8_log_bytes(tmp_path):
    manager = BackgroundProcessManager(str(tmp_path))
    log_path = tmp_path / "mixed.log"
    log_path.write_bytes("中文日志".encode("gb18030"))

    class _Proc:
        @staticmethod
        def poll():
            return 0

        returncode = 0

    meta = process_module.BackgroundProcess(
        process_id="proc_test",
        command="demo",
        cwd=str(tmp_path),
        log_path=str(log_path),
        pid=123,
        started_at="2026-06-13 00:00:00",
    )
    manager._processes["proc_test"] = (_Proc(), meta, None)

    output = manager.read_output("proc_test", tail_lines=20)
    assert "中文日志" in output


def test_stop_process_uses_taskkill_tree_on_windows(tmp_path, monkeypatch):
    manager = BackgroundProcessManager(str(tmp_path))
    captured = {}

    class _Proc:
        pid = 12345

        @staticmethod
        def poll():
            return None

        @staticmethod
        def wait(timeout=None):
            captured["wait_timeout"] = timeout

    class _Completed:
        returncode = 0

    monkeypatch.setattr(process_module.os, "name", "nt")

    def _fake_run(*args, **kwargs):
        captured["args"] = args[0]
        captured["kwargs"] = kwargs
        return _Completed()

    monkeypatch.setattr(process_module.subprocess, "run", _fake_run)

    manager._terminate_process_tree(_Proc())

    assert captured["args"] == ["taskkill", "/PID", "12345", "/T", "/F"]
    assert captured["kwargs"]["stdout"] is process_module.subprocess.DEVNULL
    assert captured["kwargs"]["stderr"] is process_module.subprocess.DEVNULL
    assert captured["wait_timeout"] == 5


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific process tree semantics")
def test_stop_process_kills_child_process_tree_on_windows(tmp_path):
    manager = BackgroundProcessManager(str(tmp_path))
    child_pid_file = Path(tmp_path) / "child.pid"
    parent_script = Path(tmp_path) / "spawn_child.py"
    parent_script.write_text(
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"pid_file = Path(r\"{child_pid_file.as_posix()}\")\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "pid_file.write_text(str(child.pid), encoding='utf-8')\n"
        "print(f'PARENT_READY:{child.pid}', flush=True)\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )

    started = manager.start_process(command=f'"{sys.executable}" "{parent_script}"', cwd=".")
    process_id = started.splitlines()[0].split()[-1]

    try:
        deadline = time.time() + 10
        while time.time() < deadline and not child_pid_file.exists():
            time.sleep(0.2)
        assert child_pid_file.exists(), "parent process did not record child pid"

        child_pid = child_pid_file.read_text(encoding="utf-8").strip()
        before = subprocess.run(
            ["tasklist", "/FI", f"PID eq {child_pid}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        ).stdout
        assert child_pid in before

        manager.stop_process(process_id)
        time.sleep(1.5)

        after = subprocess.run(
            ["tasklist", "/FI", f"PID eq {child_pid}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        ).stdout
        assert child_pid not in after
    finally:
        try:
            manager.stop_process(process_id)
        except Exception:
            pass


def test_cleanup_task_processes_stops_only_temporary_processes_for_task(tmp_path):
    manager = BackgroundProcessManager(str(tmp_path))

    class _Proc:
        def __init__(self, pid):
            self.pid = pid

        @staticmethod
        def poll():
            return 0

    class _Log:
        def close(self):
            return None

    manager._processes = {
        "proc_temp": (
            _Proc(101),
            process_module.BackgroundProcess(
                process_id="proc_temp",
                command="temp",
                cwd=str(tmp_path),
                log_path=str(tmp_path / "temp.log"),
                pid=101,
                started_at="2026-06-13 00:00:00",
                task_id="task_1",
                keep_alive=False,
            ),
            _Log(),
        ),
        "proc_keep": (
            _Proc(102),
            process_module.BackgroundProcess(
                process_id="proc_keep",
                command="keep",
                cwd=str(tmp_path),
                log_path=str(tmp_path / "keep.log"),
                pid=102,
                started_at="2026-06-13 00:00:00",
                task_id="task_1",
                keep_alive=True,
            ),
            _Log(),
        ),
        "proc_other_task": (
            _Proc(103),
            process_module.BackgroundProcess(
                process_id="proc_other_task",
                command="other",
                cwd=str(tmp_path),
                log_path=str(tmp_path / "other.log"),
                pid=103,
                started_at="2026-06-13 00:00:00",
                task_id="task_2",
                keep_alive=False,
            ),
            _Log(),
        ),
    }

    stopped = manager.cleanup_task_processes("task_1")

    assert stopped == ["proc_temp"]
    assert "proc_temp" not in manager._processes
    assert "proc_keep" in manager._processes
    assert "proc_other_task" in manager._processes


def test_cleanup_all_stops_persistent_processes_on_manager_close(tmp_path):
    manager = BackgroundProcessManager(str(tmp_path))

    class _Proc:
        def __init__(self, pid):
            self.pid = pid

        @staticmethod
        def poll():
            return 0

    class _Log:
        def close(self):
            return None

    manager._processes = {
        "proc_temp": (
            _Proc(201),
            process_module.BackgroundProcess(
                process_id="proc_temp",
                command="temp",
                cwd=str(tmp_path),
                log_path=str(tmp_path / "temp.log"),
                pid=201,
                started_at="2026-06-13 00:00:00",
                task_id="task_1",
                keep_alive=False,
            ),
            _Log(),
        ),
        "proc_keep": (
            _Proc(202),
            process_module.BackgroundProcess(
                process_id="proc_keep",
                command="keep",
                cwd=str(tmp_path),
                log_path=str(tmp_path / "keep.log"),
                pid=202,
                started_at="2026-06-13 00:00:00",
                task_id="task_1",
                keep_alive=True,
            ),
            _Log(),
        ),
    }

    stopped = manager.cleanup_all(include_persistent=True)

    assert stopped == ["proc_temp", "proc_keep"]
    assert manager._processes == {}


def test_policy_allows_background_process(tmp_path):
    policy = Policy(workspace_root=str(tmp_path), permission_mode="ask")
    decision = policy.evaluate_tool_call("start_process", {"command": "python receive.py"})
    assert decision.action == "allow"
