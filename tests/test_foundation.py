from pathlib import Path

from autocode.context import MemoryManager, normalize_todos, render_todos
from autocode.infra import WorkspaceFS, Sandbox
from autocode.infra import sandbox as sandbox_module
from autocode.state import TaskState
from autocode.state import TaskStore
from autocode.state import checkpoint as checkpoint_module


def test_workspace_fs_stays_inside_workspace(tmp_path):
    fs = WorkspaceFS(str(tmp_path))
    assert fs.resolve_path("a.txt") == tmp_path.joinpath("a.txt").resolve()
    try:
        fs.read_text(str(tmp_path.parent / "outside.txt"))
    except ValueError:
        pass
    else:
        raise AssertionError("expected a workspace boundary error")


def test_workspace_fs_reads_and_writes_utf8(tmp_path):
    fs = WorkspaceFS(str(tmp_path))
    fs.write_text("utf8.txt", "你好，Redis Session")
    assert fs.read_text("utf8.txt") == "你好，Redis Session"


def test_sandbox_tracks_cwd(tmp_path):
    sandbox = Sandbox(str(tmp_path))
    child = tmp_path / "child"
    child.mkdir()
    sandbox.run("cd child && python -c \"print('ok')\"")
    assert Path(sandbox.cwd) == child.resolve()


def test_sandbox_decodes_utf8_subprocess_output(tmp_path, monkeypatch):
    captured = {}

    class _Proc:
        returncode = 0
        stdout = "hello 世界"
        stderr = ""

    def _fake_run(*args, **kwargs):
        captured.update(kwargs)
        return _Proc()

    monkeypatch.setattr(sandbox_module.subprocess, "run", _fake_run)

    result = Sandbox(str(tmp_path)).run("python -c \"print('hello')\"")
    assert result.output == "hello 世界"
    assert captured["text"] is True
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"


def test_memory_manager_reads_project_files(tmp_path, monkeypatch):
    (tmp_path / "AGENTS.md").write_text("rule-one")
    (tmp_path / ".autocode").mkdir()
    (tmp_path / ".autocode" / "PROJECT_MEMORY.md").write_text("project-one", encoding="utf-8")
    manager = MemoryManager(str(tmp_path))
    block = manager.build_memory_block()
    assert "rule-one" in block
    assert "project-one" in block


def test_todo_helpers_render():
    todos = normalize_todos([
        {"content": "Read file", "status": "pending"},
        {"content": "Edit file", "status": "completed"},
    ])
    text = render_todos(todos)
    assert "[ ] Read file" in text
    assert "[x] Edit file" in text


def test_task_store_sync(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "TASKS_DIR", tmp_path)
    state = TaskState(task_id="task_foundation", title="demo", todos=[{"content": "x", "status": "pending"}])
    store = TaskStore()
    store.sync(state, "demo-model")
    data = store.load("task_foundation")
    assert data is not None
    assert data["title"] == "demo"
    assert len(data["todos"]) == 1

