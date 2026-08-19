"""Workspace-bound content search powered by ripgrep."""

from __future__ import annotations

import shutil
from pathlib import Path

from .base import ConcurrencySpec, Tool

_MAX_HEAD_LIMIT = 200
_MAX_COLLECTED_LINES = 10_000
_SEARCH_TIMEOUT_SECONDS = 30
_SKIP_DIRS = (".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", "dist", "build")
_OUTPUT_MODES = {"files_with_matches", "content", "count"}


class GrepTool(Tool):
    name = "grep"
    description = (
        "Search with ripgrep regular expressions. Supports files_with_matches, content, and count "
        "output modes plus glob/type filters, multiline matching, and pagination."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "ripgrep regular expression"},
            "path": {"type": "string", "description": "File or directory (default workspace)"},
            "output_mode": {
                "type": "string",
                "enum": sorted(_OUTPUT_MODES),
                "description": "Default files_with_matches.",
            },
            "glob": {"type": "string", "description": "Filter files with a glob, e.g. '*.py'"},
            "type": {"type": "string", "description": "ripgrep file type, e.g. 'py' or 'js'"},
            "multiline": {"type": "boolean", "description": "Enable multiline matching"},
            "offset": {"type": "integer", "minimum": 0, "description": "Entries to skip"},
            "head_limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_HEAD_LIMIT,
                "description": "Maximum entries to return. Default 100.",
            },
        },
        "required": ["pattern"],
    }

    def concurrency_spec(self, arguments: dict) -> ConcurrencySpec:
        return ConcurrencySpec.parallel("read-only workspace search")

    def execute(
        self,
        pattern: str,
        path: str = ".",
        output_mode: str = "files_with_matches",
        glob: str | None = None,
        type: str | None = None,
        multiline: bool = False,
        offset: int = 0,
        head_limit: int = 100,
        include: str | None = None,
    ) -> str:
        if output_mode not in _OUTPUT_MODES:
            return f"Error: output_mode must be one of {', '.join(sorted(_OUTPUT_MODES))}"
        if offset < 0 or not 1 <= head_limit <= _MAX_HEAD_LIMIT:
            return f"Error: offset must be >= 0 and head_limit must be 1-{_MAX_HEAD_LIMIT}"
        if include and not glob:
            glob = include
        executable = shutil.which("rg")
        if executable is None:
            return "Error: ripgrep executable 'rg' is required but was not found on PATH"
        try:
            target = self._resolve_target(path)
        except ValueError as exc:
            return f"Error: {exc}"
        if not target.exists():
            return f"Error: {path} not found"

        arguments = [executable, "--no-heading", "--color=never", "--with-filename"]
        if output_mode == "files_with_matches":
            arguments.append("--files-with-matches")
        elif output_mode == "content":
            arguments.extend(("--line-number", "--max-columns=2000", "--max-columns-preview"))
        else:
            arguments.append("--count-matches")
        if multiline:
            arguments.append("--multiline")
        if glob:
            arguments.extend(("--glob", glob))
        if type:
            arguments.extend(("--type", type))
        for directory in _SKIP_DIRS:
            arguments.extend(("--glob", f"!**/{directory}/**"))
        arguments.extend(("--", str(pattern), str(target)))

        sandbox = getattr(self, "_sandbox", None)
        if sandbox is None:
            return "Error: grep requires an attached sandbox provider"
        try:
            completed = sandbox.run_argv(arguments, timeout=_SEARCH_TIMEOUT_SECONDS)
        except (OSError, RuntimeError, ValueError) as exc:
            return f"Error: failed to start ripgrep: {exc}"

        output = completed.stdout.strip()
        if completed.timed_out:
            return f"Error: search timed out after {_SEARCH_TIMEOUT_SECONDS}s"
        if completed.exit_code == 1:
            return "No matches found."
        if completed.exit_code != 0:
            diagnostic = (completed.stderr or output).strip()
            return f"Invalid regex or ripgrep error: {diagnostic or f'exit code {completed.exit_code}'}"
        lines = output.splitlines()
        collection_truncated = len(lines) > _MAX_COLLECTED_LINES
        lines = lines[:_MAX_COLLECTED_LINES]
        total_entries = len(lines)
        shown = lines[offset:offset + head_limit]
        if not shown:
            return f"No entries at offset {offset}. Total entries: {total_entries}."
        result = "\n".join(shown)
        if output_mode == "count":
            total_matches = 0
            for line in lines:
                try:
                    total_matches += int(line.rsplit(":", 1)[1])
                except (IndexError, ValueError):
                    continue
            result += f"\n\nTotal matches: {total_matches}"
        if collection_truncated or offset + len(shown) < total_entries:
            known_total = f"at least {total_entries}" if collection_truncated else str(total_entries)
            result += (
                f"\n\nPARTIAL results: {known_total} entries, showing {offset + 1}-"
                f"{offset + len(shown)}; next_offset={offset + len(shown)}."
            )
        return result

    def _resolve_target(self, path: str) -> Path:
        fs = getattr(self, "_fs", None)
        if fs is not None:
            return fs.resolve_path(path)
        return Path(path).expanduser().resolve()
