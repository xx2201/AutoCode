"""File pattern matching with brace expansion and explicit pagination."""

from __future__ import annotations

import re
from pathlib import Path

from .base import ConcurrencySpec, Tool

_MAX_RESULTS = 100


class GlobTool(Tool):
    name = "glob"
    description = (
        "Find files matching a glob pattern. Supports ** recursion and brace alternatives such "
        "as '*.{json,yaml}'. Results are sorted by modification time, newest first."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern"},
            "path": {"type": "string", "description": "Directory to search (default workspace)"},
            "offset": {
                "type": "integer",
                "minimum": 0,
                "description": "Number of matching files to skip. Default 0.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_RESULTS,
                "description": "Maximum results to return. Default 100.",
            },
        },
        "required": ["pattern"],
    }

    def concurrency_spec(self, arguments: dict) -> ConcurrencySpec:
        return ConcurrencySpec.parallel("read-only workspace discovery")

    def execute(self, pattern: str, path: str = ".", offset: int = 0, limit: int = 100) -> str:
        try:
            if offset < 0 or not 1 <= limit <= _MAX_RESULTS:
                return f"Error: offset must be >= 0 and limit must be 1-{_MAX_RESULTS}"
            patterns = _expand_braces(pattern)
            fs = getattr(self, "_fs", None)
            hits: dict[str, Path] = {}
            if fs:
                for expanded in patterns:
                    for hit in fs.glob(expanded, path=path):
                        hits[str(hit)] = hit
            else:
                base = Path(path).expanduser().resolve()
                if not base.is_dir():
                    return f"Error: {path} is not a directory"
                for expanded in patterns:
                    for hit in base.glob(expanded):
                        hits[str(hit.resolve())] = hit.resolve()
            ordered = sorted(
                hits.values(),
                key=lambda item: item.stat().st_mtime if item.exists() else 0,
                reverse=True,
            )
            total = len(ordered)
            shown = ordered[offset:offset + limit]
            if not shown:
                return "No files matched." if total == 0 else f"No results at offset {offset}. Total: {total}."
            result = "\n".join(str(item) for item in shown)
            if offset + len(shown) < total:
                result += (
                    f"\n\nPARTIAL results: {total} matches, showing {offset + 1}-"
                    f"{offset + len(shown)}; next_offset={offset + len(shown)}."
                )
            return result
        except Exception as exc:
            return f"Error: {exc}"


def _expand_braces(pattern: str) -> list[str]:
    match = re.search(r"\{([^{}]+)\}", pattern)
    if match is None:
        return [pattern]
    choices = match.group(1).split(",")
    if not choices or any(choice == "" for choice in choices):
        raise ValueError("brace glob alternatives must not be empty")
    expanded: list[str] = []
    for choice in choices:
        replaced = pattern[:match.start()] + choice + pattern[match.end():]
        expanded.extend(_expand_braces(replaced))
    return expanded
