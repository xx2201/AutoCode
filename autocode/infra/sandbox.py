"""Minimal shell sandbox for command execution."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SandboxResult:
    output: str
    exit_code: int
    cwd: str


class Sandbox:
    def __init__(self, workspace_root: str):
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.cwd = str(self.workspace_root)

    def run(self, command: str, timeout: int = 120) -> SandboxResult:
        cwd = self.cwd
        try:
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=cwd,
                env=self._build_env(),
            )
        except subprocess.TimeoutExpired as e:
            raise TimeoutError(str(e)) from e
        if proc.returncode == 0:
            self._update_cwd(command, cwd)

        out = proc.stdout
        if proc.stderr:
            out += f"\n[stderr]\n{proc.stderr}"
        if proc.returncode != 0:
            out += f"\n[exit code: {proc.returncode}]"
        if len(out) > 15_000:
            out = out[:6000] + f"\n\n... truncated ({len(out)} chars total) ...\n\n" + out[-3000:]

        return SandboxResult(output=out.strip() or "(no output)", exit_code=proc.returncode, cwd=self.cwd)

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
        }
        return {k: v for k, v in os.environ.items() if k.upper() in allowed}

    def _update_cwd(self, command: str, current_cwd: str):
        parts = command.split("&&")
        for part in parts:
            part = part.strip()
            if part.startswith("cd "):
                target = part[3:].strip().strip("'\"")
                if target:
                    new_dir = Path(current_cwd).joinpath(Path(target).expanduser()).resolve()
                    if new_dir.is_dir():
                        try:
                            new_dir.relative_to(self.workspace_root)
                        except ValueError:
                            continue
                        self.cwd = str(new_dir)

