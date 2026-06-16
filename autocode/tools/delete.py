"""Workspace file and directory deletion."""

from pathlib import Path

from .base import Tool


class DeletePathTool(Tool):
    name = "delete_path"
    description = (
        "Delete a file or directory inside the workspace. "
        "Requires user approval before execution."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace path to delete",
            },
            "recursive": {
                "type": "boolean",
                "description": "Set true to delete a non-empty directory",
            },
        },
        "required": ["path"],
    }

    def execute(self, path: str, recursive: bool = False) -> str:
        try:
            fs = getattr(self, "_fs", None)
            if fs:
                target = fs.resolve_path(path)
                was_dir = target.is_dir()
                target = fs.delete_path(path, recursive=recursive)
            else:
                target = Path(path).expanduser().resolve()
                if not target.exists():
                    return f"Error: {path} not found"
                was_dir = target.is_dir()
                if target.is_dir():
                    if recursive:
                        import shutil
                        shutil.rmtree(target)
                    else:
                        target.rmdir()
                else:
                    target.unlink()
            kind = "directory" if was_dir else "file"
            return f"Deleted {kind} {path}"
        except Exception as e:
            return f"Error: {e}"
