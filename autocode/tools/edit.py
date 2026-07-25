"""Search-and-replace file editing (Claude Code's key innovation).

The core idea: instead of sending whole-file rewrites or line-number patches,
the LLM specifies an *exact* substring to find and its replacement. The
substring must appear exactly once in the file, which eliminates ambiguity
and makes edits safe and reviewable.
"""

import difflib

from .base import Tool
from .file_state import DEFAULT_FILE_READ_TRACKER

# track files changed this session for /diff
_changed_files: set[str] = set()


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "Edit a file by replacing an exact string match. "
        "You must call read on the complete file first. If the file later changes, the edit is "
        "still allowed only when old_string remains exact and unambiguous. old_string must be "
        "unique unless replace_all is true."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to the file to edit",
            },
            "old_string": {
                "type": "string",
                "description": "Exact text to find (must be unique in file)",
            },
            "new_string": {
                "type": "string",
                "description": "Replacement text",
            },
            "replace_all": {
                "type": "boolean",
                "description": "Replace every occurrence of old_string instead of requiring one unique match",
                "default": False,
            },
        },
        "required": ["file_path", "old_string", "new_string"],
    }

    def execute(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> str:
        try:
            fs = getattr(self, "_fs", None)
            if fs:
                p = fs.resolve_path(file_path)
                if not p.exists():
                    return f"Error: {file_path} not found"
                content = fs.read_text(file_path)
            else:
                from pathlib import Path
                p = Path(file_path).expanduser().resolve()
                if not p.exists():
                    return f"Error: {file_path} not found"
                content = p.read_text()
            tracker = getattr(self, "_file_read_tracker", DEFAULT_FILE_READ_TRACKER)
            read_status = tracker.status(p, content)
            if read_status == "unread":
                return f"Error: read must be called on the complete {file_path} before edit_file"
            if not old_string:
                return "Error: old_string must not be empty"
            occurrences = content.count(old_string)

            if occurrences == 0:
                preview = content[:500] + ("..." if len(content) > 500 else "")
                return (
                    f"Error: old_string not found in {file_path}.\n"
                    f"File starts with:\n{preview}"
                )
            if occurrences > 1 and not replace_all:
                return (
                    f"Error: old_string appears {occurrences} times in {file_path}. "
                    "Include more surrounding lines to make it unique or set replace_all=true."
                )

            replacement_count = occurrences if replace_all else 1
            new_content = content.replace(old_string, new_string, replacement_count)
            if fs:
                fs.write_text(file_path, new_content)
            else:
                p.write_text(new_content)
            if read_status == "changed":
                tracker.forget(p)
            else:
                tracker.record(p, new_content)
            _changed_files.add(str(p))

            # The tool result remains machine-readable for the model; the web UI renders its own diff.
            diff = _unified_diff(content, new_content, str(p))
            warning = ""
            if read_status == "changed":
                warning = (
                    "Warning: the file had other changes after it was read. The exact edit was "
                    "applied; call read again before another edit.\n"
                )
            return f"Edited {file_path} ({replacement_count} replacement(s))\n{warning}{diff}"
        except Exception as e:
            return f"Error: {e}"


def _unified_diff(old: str, new: str, filename: str, context: int = 3) -> str:
    """Generate a compact unified diff between old and new file content."""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{filename}", tofile=f"b/{filename}",
        n=context,
    )
    result = "".join(diff)
    # truncate enormous diffs
    if len(result) > 3000:
        result = result[:2500] + "\n... (diff truncated)\n"
    return result
