"""Workspace-scoped command execution with an explicit shell provider."""

from __future__ import annotations

import locale
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from .process_control import process_group_options, terminate_process_tree
from .shell import create_shell_provider


@dataclass
class SandboxResult:
    stdout: str
    stderr: str
    exit_code: int
    cwd: str
    shell: str
    timed_out: bool = False
    truncated: bool = False
    full_output_path: str | None = None

    def to_dict(self) -> dict:
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "truncated": self.truncated,
            "full_output_path": self.full_output_path,
            "cwd": self.cwd,
            "shell": self.shell,
        }


def decode_output(data: bytes | None) -> str:
    if not data:
        return ""

    encodings = ["utf-8", locale.getpreferredencoding(False), "gb18030"]
    tried = set()
    for encoding in encodings:
        normalized = (encoding or "").strip()
        if not normalized or normalized.lower() in tried:
            continue
        tried.add(normalized.lower())
        try:
            return data.decode(normalized)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


class Sandbox:
    def __init__(self, workspace_root: str):
        self.workspace_root = Path(workspace_root).expanduser().resolve()
    def run(
        self,
        command: str,
        timeout: int = 120,
        workdir: str = ".",
        shell: str | None = None,
    ) -> SandboxResult:
        cwd = self._resolve_workdir(workdir)
        provider = create_shell_provider(shell)
        invocation = provider.invocation(command)
        popen_options = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "cwd": str(cwd),
            "env": self._build_env(),
            **process_group_options(),
        }
        proc = subprocess.Popen(invocation.argv(), **popen_options)
        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate_process_tree(proc, env=popen_options["env"])
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                terminate_process_tree(proc, env=popen_options["env"])
                stdout, stderr = proc.communicate()

        stdout_text = decode_output(stdout)
        stderr_text = decode_output(stderr)
        full_output_path = None
        truncated = len(stdout_text) + len(stderr_text) > 15_000
        if truncated:
            full_output_path = self._persist_full_output(
                stdout=stdout_text,
                stderr=stderr_text,
            )
            stdout_text = _truncate(stdout_text)
            stderr_text = _truncate(stderr_text)

        return SandboxResult(
            stdout=stdout_text,
            stderr=stderr_text,
            exit_code=proc.returncode if proc.returncode is not None else -1,
            cwd=str(cwd),
            shell=invocation.name,
            timed_out=timed_out,
            truncated=truncated,
            full_output_path=full_output_path,
        )

    @staticmethod
    def classify(command: str) -> str:
        command = command.strip().lower()
        if not command:
            return "empty"
        if command.startswith(("git status", "git diff", "ls", "dir", "pwd", "rg ", "grep ", "cat ", "type ")):
            return "read_only"
        if command.startswith(("pytest", "python -m pytest")):
            return "verification"
        return "mutating"

    @staticmethod
    def _build_env() -> dict[str, str]:
        allowed = {
            "PATH",
            "PATHEXT",
            "SYSTEMROOT",
            "WINDIR",
            "HOME",
            "USERPROFILE",
            "TMP",
            "TEMP",
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "AUTOCODE_API_KEY",
            "AUTOCODE_BASE_URL",
            "AUTOCODE_MODEL",
            "PYTHONPATH",
            "VIRTUAL_ENV",
            "CLAUDE_CODE_GIT_BASH_PATH",
        }
        env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        return env

    def _resolve_workdir(self, workdir: str) -> Path:
        path = Path(workdir or ".").expanduser()
        if not path.is_absolute():
            path = self.workspace_root / path
        path = path.resolve()
        try:
            path.relative_to(self.workspace_root)
        except ValueError as exc:
            raise ValueError(f"workdir must stay inside workspace: {self.workspace_root}") from exc
        if not path.is_dir():
            raise ValueError(f"workdir is not a directory: {path}")
        return path

    def _persist_full_output(self, *, stdout: str, stderr: str) -> str:
        output_dir = self.workspace_root / ".autocode" / "tool-output"
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"shell-{uuid.uuid4().hex}.log"
        body = f"[stdout]\n{stdout}\n\n[stderr]\n{stderr}"
        path.write_text(body, encoding="utf-8")
        return str(path)


def _truncate(text: str, head: int = 6000, tail: int = 3000) -> str:
    if len(text) <= head + tail:
        return text
    return text[:head] + f"\n\n... truncated ({len(text)} chars total) ...\n\n" + text[-tail:]

