"""Explicit platform shell providers."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ShellInvocation:
    """One fully resolved native process invocation."""

    executable: str
    arguments: tuple[str, ...]
    name: str

    def argv(self) -> list[str]:
        return [self.executable, *self.arguments]


class ShellProvider:
    """Build an argv without delegating interpreter selection to ``shell=True``."""

    name: str

    def invocation(self, command: str) -> ShellInvocation:
        raise NotImplementedError


class PowerShellProvider(ShellProvider):
    name = "powershell"

    def __init__(self) -> None:
        executable = shutil.which("pwsh") or shutil.which("powershell")
        if executable is None:
            raise RuntimeError("PowerShell was not found. Install pwsh or enable Windows PowerShell.")
        self.executable = executable

    def invocation(self, command: str) -> ShellInvocation:
        return ShellInvocation(
            executable=self.executable,
            arguments=("-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command),
            name=self.name,
        )


class BashProvider(ShellProvider):
    name = "bash"

    def __init__(self) -> None:
        executable = _resolve_bash()
        if executable is None:
            raise RuntimeError(
                "Bash was not found. On Windows, install Git for Windows or set "
                "CLAUDE_CODE_GIT_BASH_PATH to bash.exe."
            )
        self.executable = executable

    def invocation(self, command: str) -> ShellInvocation:
        return ShellInvocation(
            executable=self.executable,
            arguments=("-lc", command),
            name=self.name,
        )


def default_shell_name() -> str:
    return "powershell" if os.name == "nt" else "bash"


def available_shell_names() -> tuple[str, ...]:
    if os.name == "nt":
        return ("powershell", "bash")
    return ("bash",)


def create_shell_provider(name: str | None = None) -> ShellProvider:
    selected = (name or default_shell_name()).strip().lower()
    if selected == "powershell" and os.name == "nt":
        return PowerShellProvider()
    if selected == "bash":
        return BashProvider()
    supported = ", ".join(available_shell_names())
    raise ValueError(f"Unsupported shell '{selected}'. Expected one of: {supported}.")


def _resolve_bash() -> str | None:
    configured = os.getenv("CLAUDE_CODE_GIT_BASH_PATH", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return str(path.resolve())
        raise RuntimeError(f"CLAUDE_CODE_GIT_BASH_PATH does not point to a file: {path}")
    return shutil.which("bash")
