"""Workspace-scoped command execution with an explicit shell provider."""

from __future__ import annotations

import locale
import os
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from .process_control import process_group_options, terminate_process_tree
from .sandbox_policy import SandboxExecutionPolicy, SandboxPolicy
from .shell import create_shell_provider


@dataclass
class SandboxResult:
    stdout: str
    stderr: str
    exit_code: int
    cwd: str
    shell: str
    sandbox_mode: str
    sandbox_enforcement: str
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
            "sandbox_mode": self.sandbox_mode,
            "sandbox_enforcement": self.sandbox_enforcement,
        }


@dataclass(frozen=True)
class SandboxInvocation:
    argv: list[str]
    cwd: Path
    env: dict[str, str]
    shell: str
    policy: SandboxExecutionPolicy
    enforcement: str


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
    def __init__(self, workspace_root: str, policy: SandboxPolicy | None = None):
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.policy = policy or SandboxPolicy(str(self.workspace_root))

    def prepare(
        self,
        command: str,
        workdir: str = ".",
        shell: str | None = None,
        mode: str | None = None,
    ) -> SandboxInvocation:
        cwd = self._resolve_workdir(workdir)
        provider = create_shell_provider(shell)
        invocation = provider.invocation(command)
        argv = invocation.argv()
        if invocation.name == "powershell" and self.policy.resolve(mode).mode != "danger-full-access":
            if Path(invocation.executable).name.lower() != "pwsh.exe":
                raise RuntimeError(
                    "the restricted Windows sandbox requires PowerShell 7 (pwsh.exe)"
                )
            argv[1:1] = ["-WorkingDirectory", str(cwd)]
        return self._prepare_argv(argv, cwd=cwd, name=invocation.name, mode=mode)

    def prepare_argv(
        self,
        argv: list[str],
        workdir: str = ".",
        mode: str | None = None,
    ) -> SandboxInvocation:
        if not argv or not str(argv[0]).strip():
            raise ValueError("argv must contain an executable")
        cwd = self._resolve_workdir(workdir)
        return self._prepare_argv([str(item) for item in argv], cwd=cwd, name="direct", mode=mode)

    def _prepare_argv(
        self,
        argv: list[str],
        *,
        cwd: Path,
        name: str,
        mode: str | None,
    ) -> SandboxInvocation:
        policy = self.policy.resolve(mode)
        enforcement = "none"
        if policy.mode != "danger-full-access":
            if os.name != "nt":
                raise RuntimeError(
                    f'sandbox mode "{policy.mode}" requires the Windows ACL backend; '
                    "refusing to run the command unconfined"
                )
            runner = Path(__file__).with_name("windows_runner.py")
            argv = [
                sys.executable,
                str(runner),
                "--workspace",
                str(policy.workspace_root),
                "--cwd",
                str(cwd),
                "--temp-root",
                tempfile.gettempdir(),
                "--mode",
                policy.mode,
                "--",
                *argv,
            ]
            enforcement = "partial"
        return SandboxInvocation(
            argv=argv,
            cwd=cwd,
            env=self._build_env(),
            shell=name,
            policy=policy,
            enforcement=enforcement,
        )

    def run(
        self,
        command: str,
        timeout: int = 120,
        workdir: str = ".",
        shell: str | None = None,
        mode: str | None = None,
    ) -> SandboxResult:
        prepared = self.prepare(command, workdir=workdir, shell=shell, mode=mode)
        return self._run_prepared(prepared, timeout)

    def run_argv(
        self,
        argv: list[str],
        timeout: int = 120,
        workdir: str = ".",
        mode: str | None = None,
    ) -> SandboxResult:
        prepared = self.prepare_argv(argv, workdir=workdir, mode=mode)
        return self._run_prepared(prepared, timeout)

    def _run_prepared(self, prepared: SandboxInvocation, timeout: int) -> SandboxResult:
        popen_options = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "cwd": str(prepared.cwd),
            "env": prepared.env,
            **process_group_options(),
        }
        proc = subprocess.Popen(prepared.argv, **popen_options)
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
            output_dir = self.workspace_root / ".autocode" / "tool-output"
            if prepared.policy.can_write(output_dir):
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
            cwd=str(prepared.cwd),
            shell=prepared.shell,
            sandbox_mode=prepared.policy.mode,
            sandbox_enforcement=prepared.enforcement,
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

