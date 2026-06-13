"""Minimal shell sandbox for command execution."""

from __future__ import annotations

import locale
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SandboxResult:
    output: str
    exit_code: int
    cwd: str


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
        self.cwd = str(self.workspace_root)

    def run(self, command: str, timeout: int = 120) -> SandboxResult:
        cwd = self.cwd
        try:
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                timeout=timeout,
                cwd=cwd,
                env=self._build_env(),
            )
        except subprocess.TimeoutExpired as e:
            raise TimeoutError(str(e)) from e
        if proc.returncode == 0:
            self._update_cwd(command, cwd)

        out = decode_output(proc.stdout)
        if proc.stderr:
            out += f"\n[stderr]\n{decode_output(proc.stderr)}"
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
        env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        return env

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

