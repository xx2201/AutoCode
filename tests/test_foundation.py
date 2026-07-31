import hashlib
import json
import sys
from pathlib import Path

from autocode.context import MemoryManager, normalize_todos, render_todos
from autocode.infra import WorkspaceFS, Sandbox
from autocode.infra import sandbox as sandbox_module
from autocode.state import SessionState
from autocode.state import TaskState
from autocode.state import SessionStore
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


def test_sandbox_uses_explicit_workdir_without_shared_cwd(tmp_path):
    sandbox = Sandbox(str(tmp_path))
    child = tmp_path / "child"
    child.mkdir()
    result = sandbox.run(
        "python -c \"import os; print(os.getcwd())\"",
        workdir="child",
    )
    assert Path(result.cwd) == child.resolve()
    assert str(child.resolve()) in result.stdout
    root_result = sandbox.run("python -c \"import os; print(os.getcwd())\"")
    assert Path(root_result.cwd) == tmp_path.resolve()


def test_sandbox_decodes_utf8_subprocess_output(tmp_path, monkeypatch):
    captured = {}

    class _Proc:
        returncode = 0

        @staticmethod
        def communicate(timeout=None):
            captured["timeout"] = timeout
            return "hello 世界".encode("utf-8"), b""

    def _fake_popen(*args, **kwargs):
        captured.update(kwargs)
        return _Proc()

    monkeypatch.setattr(sandbox_module.subprocess, "Popen", _fake_popen)

    result = Sandbox(str(tmp_path)).run("python -c \"print('hello')\"")
    assert result.stdout == "hello 世界"
    assert "text" not in captured
    assert "encoding" not in captured
    assert "errors" not in captured
    assert captured["timeout"] == 120


def test_decode_output_falls_back_to_gb18030():
    text = "中文输出"
    assert sandbox_module.decode_output(text.encode("gb18030")) == text


def test_sandbox_build_env_forces_python_utf8(monkeypatch):
    monkeypatch.setenv("PATH", "demo-path")
    env = Sandbox._build_env()
    assert env["PATH"] == "demo-path"
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["PYTHONUTF8"] == "1"


def test_sandbox_preserves_utf8_from_child_python(tmp_path):
    sandbox = Sandbox(str(tmp_path))
    command = f'& "{sys.executable}" -c "print(\'预计耗时 3 秒\')"'
    result = sandbox.run(command)
    assert "预计耗时 3 秒" in result.stdout


def test_memory_manager_reads_project_files(tmp_path, monkeypatch):
    (tmp_path / "AGENTS.md").write_text("rule-one")
    manager = MemoryManager(str(tmp_path))
    memory_path = manager.memory_file_path()
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "README.md"
    source.write_text("project-one", encoding="utf-8")
    memory_path.write_text(
        json.dumps(
            [
                {
                    "fact": "project-one",
                    "source": "README.md",
                    "source_hash": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "confidence": "verified",
                    "scope": "project",
                    "invalidated": False,
                }
            ]
        ),
        encoding="utf-8",
    )
    assert "rule-one" in manager.build_rules_block()
    assert "project-one" in manager.build_project_memory_block()


def test_todo_helpers_render():
    todos = normalize_todos([
        {"content": "Read file", "status": "pending"},
        {"content": "Edit file", "status": "completed"},
    ])
    text = render_todos(todos)
    assert "[ ] Read file" in text
    assert "[x] Edit file" in text


def test_session_store_sync(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint_module, "SESSIONS_DIR", tmp_path)
    state = SessionState(
        session_id="session_foundation",
        current_task=TaskState(task_id="task_foundation", title="demo", todos=[{"content": "x", "status": "pending"}]),
    )
    store = SessionStore()
    store.sync(state, "demo-model")
    data = store.load("session_foundation")
    assert data is not None
    assert data["session_id"] == "session_foundation"
    assert data["task_id"] == "task_foundation"
    assert data["title"] == "demo"

