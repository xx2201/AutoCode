"""Cross-platform process-group lifecycle helpers."""

from __future__ import annotations

import os
import signal
import subprocess


def process_group_options() -> dict[str, object]:
    """Return Popen options that make the launched process tree addressable."""
    options: dict[str, object] = {"start_new_session": True}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return options


def terminate_process_tree(
    proc: subprocess.Popen,
    *,
    env: dict[str, str] | None = None,
    command_timeout: float = 15,
    wait_timeout: float = 5,
) -> None:
    """Terminate a process and all descendants without waiting indefinitely."""
    if proc.poll() is not None:
        return

    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                timeout=command_timeout,
            )
        except subprocess.TimeoutExpired:
            completed = None
        if completed is None or completed.returncode != 0:
            _kill_single_process(proc, wait_timeout)
            return
        _wait_for_exit(proc, wait_timeout)
        return

    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=wait_timeout)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        _wait_for_exit(proc, wait_timeout)


def _kill_single_process(proc: subprocess.Popen, timeout: float) -> None:
    if proc.poll() is not None:
        return
    proc.kill()
    _wait_for_exit(proc, timeout)


def _wait_for_exit(proc: subprocess.Popen, timeout: float) -> None:
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return
