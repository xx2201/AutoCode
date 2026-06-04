from pathlib import Path

from corecoder.filesystem import WorkspaceFS
from corecoder.memory import MemoryManager
from corecoder.sandbox import Sandbox
from corecoder.tasks import TaskStore
from corecoder.todo import normalize_todos, render_todos
from corecoder.state import TaskState
from corecoder import checkpoint as checkpoint_module


def test_workspace_fs_stays_inside_workspace(tmp_path):
    fs = WorkspaceFS(str(tmp_path))
    assert fs.resolve_path("a.txt") == tmp_path.joinpath("a.txt").resolve()
    try:
        fs.read_text(str(tmp_path.parent / "outside.txt"))
    except ValueError:
        pass
    else:
        raise AssertionError("expected a workspace boundary error")


def test_sandbox_tracks_cwd(tmp_path):
    sandbox = Sandbox(str(tmp_path))
    child = tmp_path / "child"
    child.mkdir()
    sandbox.run("cd child && python -c \"print('ok')\"")
    assert Path(sandbox.cwd) == child.resolve()


def test_memory_manager_reads_project_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENTS.md").write_text("rule-one")
    (tmp_path / ".corecoder").mkdir()
    (tmp_path / ".corecoder" / "memory.md").write_text("memory-one")
    manager = MemoryManager(str(tmp_path))
    block = manager.build_memory_block()
    assert "rule-one" in block
    assert "memory-one" in block


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
