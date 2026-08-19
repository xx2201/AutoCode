"""Workspace-scoped background process manager."""

from __future__ import annotations

import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .filesystem import WorkspaceFS
from .process_control import process_group_options, terminate_process_tree
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
    shell: str
    turn_id: str = ""
    keep_alive: bool = False


class BackgroundProcessManager:
    def __init__(self, workspace_root: str, sandbox: Sandbox | None = None):
        self.sandbox = sandbox or Sandbox(workspace_root)
        self.fs = WorkspaceFS(workspace_root, policy=self.sandbox.policy)
        self._lock = threading.Lock()
        self._processes: dict[str, tuple[subprocess.Popen, BackgroundProcess, object]] = {}

    def start_process(
        self,
        command: str,
        cwd: str = ".",
        log_file: str | None = None,
        keep_alive: bool = False,
        turn_id: str | None = None,
        shell: str | None = None,
    ) -> str:
        run_cwd = self.fs.resolve_path(cwd)
        self.fs.ensure_within_workspace(run_cwd)
        if not run_cwd.is_dir():
            raise ValueError(f"{cwd} is not a directory")

        process_id = _new_process_id()
        log_path = self._resolve_log_path(log_file, process_id)
        self.fs.policy.resolve().require_write(log_path)
        prepared = self.sandbox.prepare(command, workdir=cwd, shell=shell)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("ab")

        popen_kwargs = {
            "cwd": str(prepared.cwd),
            "stdout": log_handle,
            "stderr": subprocess.STDOUT,
            "env": prepared.env,
            **process_group_options(),
        }

        try:
            proc = subprocess.Popen(prepared.argv, **popen_kwargs)
        except Exception:
            log_handle.close()
            raise
        meta = BackgroundProcess(
            process_id=process_id,
            command=command,
            cwd=str(run_cwd),
            log_path=str(log_path),
            pid=proc.pid,
            started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            shell=prepared.shell,
            turn_id=turn_id or "",
            keep_alive=bool(keep_alive),
        )
        with self._lock:
            self._processes[process_id] = (proc, meta, log_handle)
        return (
            f"Started background process {process_id}\n"
            f"PID: {proc.pid}\n"
            f"CWD: {run_cwd}\n"
            f"Shell: {prepared.shell}\n"
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
        if log_handle is not None:
            log_handle.close()
        with self._lock:
            self._processes.pop(process_id, None)
        return f"Stopped background process {process_id} (pid {meta.pid})"

    def cleanup_turn_processes(self, turn_id: str) -> list[str]:
        if not turn_id:
            return []
        return self._cleanup_matching_processes(
            lambda meta: meta.turn_id == turn_id and not meta.keep_alive
        )

    def cleanup_all(self, include_persistent: bool = True) -> list[str]:
        return self._cleanup_matching_processes(
            lambda meta: include_persistent or not meta.keep_alive
        )

    def has_running_processes(self) -> bool:
        with self._lock:
            return any(proc.poll() is None for proc, _, _ in self._processes.values())

    def _cleanup_matching_processes(self, predicate) -> list[str]:
        with self._lock:
            process_ids = [
                process_id
                for process_id, (_, meta, _) in self._processes.items()
                if predicate(meta)
            ]

        stopped: list[str] = []
        for process_id in process_ids:
            try:
                self.stop_process(process_id)
            except ValueError:
                continue
            stopped.append(process_id)
        return stopped

    def _terminate_process_tree(self, proc: subprocess.Popen) -> None:
        terminate_process_tree(proc, env=Sandbox._build_env())

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
