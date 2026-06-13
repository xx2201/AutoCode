"""Workspace-scoped background process manager."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .filesystem import WorkspaceFS
from .sandbox import Sandbox, decode_output

_SAFE_PROCESS_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class BackgroundProcess:
    process_id: str
    command: str
    cwd: str
    log_path: str
    pid: int
    started_at: str


class BackgroundProcessManager:
    def __init__(self, workspace_root: str):
        self.fs = WorkspaceFS(workspace_root)
        self._lock = threading.Lock()
        self._processes: dict[str, tuple[subprocess.Popen, BackgroundProcess, object]] = {}

    def start_process(self, command: str, cwd: str = ".", log_file: str | None = None) -> str:
        run_cwd = self.fs.resolve_path(cwd)
        self.fs.ensure_within_workspace(run_cwd)
        if not run_cwd.is_dir():
            raise ValueError(f"{cwd} is not a directory")

        process_id = _new_process_id()
        log_path = self._resolve_log_path(log_file, process_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("ab")

        popen_kwargs = {
            "shell": True,
            "cwd": str(run_cwd),
            "stdout": log_handle,
            "stderr": subprocess.STDOUT,
            "env": Sandbox._build_env(),
            "start_new_session": True,
        }
        if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        proc = subprocess.Popen(command, **popen_kwargs)
        meta = BackgroundProcess(
            process_id=process_id,
            command=command,
            cwd=str(run_cwd),
            log_path=str(log_path),
            pid=proc.pid,
            started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        with self._lock:
            self._processes[process_id] = (proc, meta, log_handle)
        return (
            f"Started background process {process_id}\n"
            f"PID: {proc.pid}\n"
            f"CWD: {run_cwd}\n"
            f"Log: {log_path}"
        )

    def read_output(self, process_id: str, tail_lines: int = 50) -> str:
        _, meta = self._get_process(process_id)
        lines = decode_output(Path(meta.log_path).read_bytes()).splitlines()
        shown = lines[-max(1, tail_lines):]
        status = self._status(process_id)
        header = f"Process: {process_id}\nStatus: {status}\nLog: {meta.log_path}"
        body = "\n".join(shown) if shown else "(no output yet)"
        return f"{header}\n\n{body}"

    def wait_for_output(self, process_id: str, pattern: str, timeout: int = 30) -> str:
        deadline = time.time() + timeout
        regex = re.compile(pattern)
        while time.time() < deadline:
            output = self.read_output(process_id, tail_lines=200)
            if regex.search(output):
                return f"Matched pattern for {process_id}\n{output}"
            time.sleep(0.2)
        return f"Error: timed out after {timeout}s waiting for pattern '{pattern}' in {process_id}"

    def stop_process(self, process_id: str) -> str:
        proc, meta, log_handle = self._get_process_bundle(process_id)
        self._terminate_process_tree(proc)
        log_handle.close()
        with self._lock:
            self._processes.pop(process_id, None)
        return f"Stopped background process {process_id} (pid {meta.pid})"

    def _terminate_process_tree(self, proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return

        if os.name == "nt":
            completed = subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=Sandbox._build_env(),
            )
            if completed.returncode == 0:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                return

            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            return

        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=5)
        except ProcessLookupError:
            return
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)

    def _resolve_log_path(self, log_file: str | None, process_id: str) -> Path:
        if log_file:
            path = self.fs.resolve_path(log_file)
            self.fs.ensure_within_workspace(path)
            return path
        root = self.fs.workspace_root / ".autocode" / "processes"
        return root / f"{process_id}.log"

    def _status(self, process_id: str) -> str:
        proc, _ = self._get_process(process_id)
        return "running" if proc.poll() is None else f"exited ({proc.returncode})"

    def _get_process(self, process_id: str) -> tuple[subprocess.Popen, BackgroundProcess]:
        proc, meta, _ = self._get_process_bundle(process_id)
        return proc, meta

    def _get_process_bundle(self, process_id: str) -> tuple[subprocess.Popen, BackgroundProcess, object]:
        with self._lock:
            item = self._processes.get(_normalize_process_id(process_id))
        if item is None:
            raise ValueError(f"Unknown process id: {process_id}")
        return item


def _normalize_process_id(process_id: str) -> str:
    name = _SAFE_PROCESS_RE.sub("-", process_id.strip()).strip(".-_")
    if not name:
        raise ValueError("Invalid process id")
    return name


def _new_process_id() -> str:
    return f"proc_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
