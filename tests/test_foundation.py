import sys
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from autocode.context import MemoryManager, normalize_todos, render_todos
from autocode.infra import BackgroundProcessManager, Sandbox, SandboxDenied, SandboxPolicy, WorkspaceFS
from autocode.infra import sandbox as sandbox_module
from autocode.state import SessionState
from autocode.state import TurnState
from autocode.state import SessionStore
from autocode.state import checkpoint as checkpoint_module


@pytest.fixture
def windows_acl_workspace():
    """Create a workspace with ordinary read/execute access for restricted tokens.

    The Codex host gives its system-temp tree process-specific ACLs that a
    nested LUA restricted token cannot use. A real project directory carries
    ordinary user read/execute access, so inherit from the repository instead
    of changing the product sandbox's host-user ACLs to accommodate the test.
    """
    root = Path(tempfile.mkdtemp(prefix=".autocode-windows-sandbox-test-", dir=Path.cwd()))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=False)


def test_workspace_fs_reads_outside_but_workspace_write_blocks_mutation(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("visible", encoding="utf-8")
    fs = WorkspaceFS(str(workspace))

    assert fs.read_text(str(outside)) == "visible"
    try:
        fs.write_text(str(outside), "blocked")
    except SandboxDenied as exc:
        assert exc.mode == "workspace-write"
    else:
        raise AssertionError("workspace-write must reject an outside mutation")
    assert outside.read_text(encoding="utf-8") == "visible"


def test_workspace_fs_modes_share_one_policy(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    policy = SandboxPolicy(str(workspace), "read-only")
    fs = WorkspaceFS(str(workspace), policy=policy)

    try:
        fs.write_text("inside.txt", "blocked")
    except SandboxDenied as exc:
        assert exc.mode == "read-only"
    else:
        raise AssertionError("read-only must reject workspace writes")

    policy.set_mode("danger-full-access")
    fs.write_text(str(outside), "allowed")
    assert outside.read_text(encoding="utf-8") == "allowed"


def test_workspace_fs_reads_and_writes_utf8(tmp_path):
    fs = WorkspaceFS(str(tmp_path))
    fs.write_text("utf8.txt", "你好，Redis Session")
    assert fs.read_text("utf8.txt") == "你好，Redis Session"


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL backend")
def test_sandbox_uses_explicit_workdir_without_shared_cwd(windows_acl_workspace):
    sandbox = Sandbox(str(windows_acl_workspace))
    child = windows_acl_workspace / "child"
    child.mkdir()
    result = sandbox.run(
        "python -c \"import os; print(os.getcwd())\"",
        workdir="child",
    )
    assert Path(result.cwd) == child.resolve()
    assert str(child.resolve()) in result.stdout
    assert result.sandbox_mode == "workspace-write"
    assert result.sandbox_enforcement == "partial"
    root_result = sandbox.run("python -c \"import os; print(os.getcwd())\"")
    assert Path(root_result.cwd) == windows_acl_workspace.resolve()


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


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL backend")
def test_windows_sandbox_enforces_actual_write_effects(windows_acl_workspace):
    workspace = windows_acl_workspace
    writer = workspace / "writer.py"
    writer.write_text(
        "import sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text('written')\n",
        encoding="utf-8",
    )
    sandbox = Sandbox(str(workspace))

    inside = workspace / "inside.txt"
    inside_result = sandbox.run(f'& "{sys.executable}" "{writer}" "{inside}"')
    assert inside_result.exit_code == 0
    assert inside.read_text(encoding="utf-8") == "written"

    outside = workspace.parent / f"{workspace.name}-outside.txt"
    outside_result = sandbox.run(f'& "{sys.executable}" "{writer}" "{outside}"')
    assert outside_result.exit_code != 0
    assert not outside.exists()
    assert "Permission" in outside_result.stderr or "denied" in outside_result.stderr.lower()


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL backend")
def test_windows_workspace_capability_sid_is_case_insensitive(windows_acl_workspace):
    from autocode.infra.windows_acl import workspace_write_sid

    original = workspace_write_sid(windows_acl_workspace)
    differently_cased = workspace_write_sid(Path(str(windows_acl_workspace).upper()))
    assert original == differently_cased


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL backend")
def test_windows_read_only_and_grandchild_inherit_sandbox(windows_acl_workspace):
    workspace = windows_acl_workspace
    child = workspace / "child.py"
    child.write_text(
        "from pathlib import Path\nPath('grandchild.txt').write_text('ok')\n",
        encoding="utf-8",
    )
    parent = workspace / "parent.py"
    parent.write_text(
        "import subprocess, sys\n"
        "result = subprocess.run([sys.executable, 'child.py'], capture_output=True, text=True)\n"
        "print(result.stdout, end='')\n"
        "print(result.stderr, end='', file=sys.stderr)\n"
        "raise SystemExit(result.returncode)\n",
        encoding="utf-8",
    )
    policy = SandboxPolicy(str(workspace), "workspace-write")
    sandbox = Sandbox(str(workspace), policy=policy)

    result = sandbox.run(f'& "{sys.executable}" "{parent}"')
    assert result.exit_code == 0
    assert (workspace / "grandchild.txt").read_text(encoding="utf-8") == "ok"

    (workspace / "grandchild.txt").unlink()
    policy.set_mode("read-only")
    denied = sandbox.run(f'& "{sys.executable}" "{parent}"')
    assert denied.exit_code != 0
    assert not (workspace / "grandchild.txt").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL backend")
def test_read_only_blocks_parent_side_output_logs_and_background_logs(windows_acl_workspace):
    policy = SandboxPolicy(str(windows_acl_workspace), "read-only")
    sandbox = Sandbox(str(windows_acl_workspace), policy=policy)
    result = sandbox.run(f'& "{sys.executable}" -c "print(\'x\' * 20000)"')

    assert result.exit_code == 0
    assert result.truncated is True
    assert result.full_output_path is None
    assert not (windows_acl_workspace / ".autocode").exists()

    manager = BackgroundProcessManager(str(windows_acl_workspace), sandbox=sandbox)
    with pytest.raises(SandboxDenied):
        manager.start_process("Write-Output blocked")


def test_read_only_blocks_project_memory_mutation(tmp_path):
    policy = SandboxPolicy(str(tmp_path), "read-only")
    manager = MemoryManager(str(tmp_path), policy=policy)
    try:
        with pytest.raises(SandboxDenied):
            manager.apply_project_memory("remember", "project_knowledge", "sandboxed")
    finally:
        manager.close()


def test_read_only_skips_automatic_project_memory_refresh(tmp_path):
    policy = SandboxPolicy(str(tmp_path), "read-only")
    manager = MemoryManager(str(tmp_path), policy=policy)

    class _LLM:
        def clone(self):
            raise AssertionError("read-only refresh must not clone or call the model")

    try:
        assert not manager.schedule_project_memory_refresh(
            [{"role": "user", "content": "remember this"}],
            _LLM(),
            force=True,
        )
    finally:
        manager.close()


def test_memory_manager_reads_rules_and_project_memory(tmp_path):
    (tmp_path / "AGENTS.md").write_text("rule-one")
    manager = MemoryManager(str(tmp_path))
    memory_path = manager.memory_file_path()
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    memory_path.write_text(
        "# Project Memory\n\n## 项目经验\n\n- project-one\n",
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
        current_turn=TurnState(turn_id="turn_foundation", title="demo", todos=[{"content": "x", "status": "pending"}]),
    )
    store = SessionStore()
    store.sync(state, "demo-model")
    data = store.load("session_foundation")
    assert data is not None
    assert data["session_id"] == "session_foundation"
    assert data["turn_id"] == "turn_foundation"
    assert data["title"] == "demo"

