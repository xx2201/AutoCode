"""File creation / overwrite."""

from .base import ConcurrencySpec, Tool
from .edit import _changed_files, _changed_files_lock
from .file_state import DEFAULT_FILE_READ_TRACKER


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "Create a new file or completely overwrite an existing one. Existing files must first "
        "be read completely and must not have changed since that read. "
        "For small edits to existing files, prefer edit_file instead."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path for the file",
            },
            "content": {
                "type": "string",
                "description": "Full file content to write",
            },
        },
        "required": ["file_path", "content"],
    }

    def concurrency_spec(self, arguments: dict) -> ConcurrencySpec:
        return ConcurrencySpec.resources(
            writes={self.file_resource(str(arguments["file_path"]))},
            reason="writes to different normalized paths are independent",
        )

    def execute(self, file_path: str, content: str) -> str:
        try:
            fs = getattr(self, "_fs", None)
            if fs:
                p = fs.resolve_path(file_path)
            else:
                from pathlib import Path
                p = Path(file_path).expanduser().resolve()
            tracker = getattr(self, "_file_read_tracker", DEFAULT_FILE_READ_TRACKER)
            if p.exists():
                current = fs.read_text(file_path) if fs else p.read_text(encoding="utf-8")
                status = tracker.status(p, current)
                if status == "unread":
                    return f"Error: read must be called on the complete {file_path} before write_file"
                if status == "changed":
                    return (
                        f"Error: {file_path} changed since it was read. "
                        "Call read again before write_file."
                    )
            if fs:
                fs.write_text(file_path, content)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
            tracker.record(p, content)
            with _changed_files_lock:
                _changed_files.add(str(p))
            n_lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
            return f"Wrote {n_lines} lines to {file_path}"
        except Exception as e:
            return f"Error: {e}"
