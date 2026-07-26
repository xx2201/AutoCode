import json
import os
import subprocess
from pathlib import Path

import pytest

from autocode.state.changes import (
    ChangeSetConflictError,
    ChangeSetError,
    ChangeSetLimitError,
    ChangeSetStore,
    ChangeSetUnavailableError,
)


def _git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "changes@example.test")
    _git(root, "config", "user.name", "ChangeSet Tests")
    (root / "modified.txt").write_bytes(b"committed\n")
    (root / "deleted.bin").write_bytes(b"\x00committed\xff")
    (root / "renamed.txt").write_bytes(b"rename me")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "baseline")
    return root


def _store(repo: Path, tmp_path: Path, **limits) -> ChangeSetStore:
    return ChangeSetStore(
        repo,
        "session-test",
        changes_root=tmp_path / "changes",
        **limits,
    )


def test_capture_undo_and_reapply_preserve_dirty_baseline(repo: Path, tmp_path: Path):
    store = _store(repo, tmp_path)
    (repo / "modified.txt").write_bytes(b"user dirty baseline\n")
    before = store.capture_before("turn-1")

    (repo / "modified.txt").write_bytes(b"agent result\x00\xff")
    (repo / "deleted.bin").unlink()
    (repo / "renamed.txt").rename(repo / "new-name.txt")
    (repo / "created.bin").write_bytes(b"\x00\x01\xff")
    manifest = store.capture_after("turn-1", before)

    assert set(manifest.changed_paths) == {
        "created.bin",
        "deleted.bin",
        "modified.txt",
        "new-name.txt",
        "renamed.txt",
    }
    assert manifest.applicable is True
    assert manifest.state == "applied"
    persisted = json.loads((tmp_path / "changes" / "turn-1" / "manifest.json").read_text())
    modified = next(item for item in persisted["files"] if item["path"] == "modified.txt")
    assert modified["before"]["sha256"]
    assert modified["after"]["sha256"]
    assert modified["before"]["blob"].startswith("before/")

    undone = store.undo("turn-1")
    assert undone.state == "undone"
    assert (repo / "modified.txt").read_bytes() == b"user dirty baseline\n"
    assert (repo / "deleted.bin").read_bytes() == b"\x00committed\xff"
    assert (repo / "renamed.txt").read_bytes() == b"rename me"
    assert not (repo / "new-name.txt").exists()
    assert not (repo / "created.bin").exists()

    reapplied = store.reapply("turn-1")
    assert reapplied.state == "applied"
    assert (repo / "modified.txt").read_bytes() == b"agent result\x00\xff"
    assert not (repo / "deleted.bin").exists()
    assert not (repo / "renamed.txt").exists()
    assert (repo / "new-name.txt").read_bytes() == b"rename me"
    assert (repo / "created.bin").read_bytes() == b"\x00\x01\xff"


def test_conflict_rejects_whole_turn_without_partial_writes(repo: Path, tmp_path: Path):
    store = _store(repo, tmp_path)
    before = store.capture_before("turn-conflict")
    (repo / "modified.txt").write_text("agent one", encoding="utf-8")
    (repo / "renamed.txt").write_text("agent two", encoding="utf-8")
    store.capture_after("turn-conflict", before)

    (repo / "renamed.txt").write_text("user later edit", encoding="utf-8")
    with pytest.raises(ChangeSetConflictError, match="renamed.txt"):
        store.undo("turn-conflict")

    assert (repo / "modified.txt").read_text(encoding="utf-8") == "agent one"
    assert (repo / "renamed.txt").read_text(encoding="utf-8") == "user later edit"
    assert store.load("turn-conflict").state == "applied"


@pytest.mark.parametrize("change_git_state", ["head", "index"])
def test_head_or_index_change_during_turn_marks_changeset_unavailable(
    repo: Path, tmp_path: Path, change_git_state: str
):
    store = _store(repo, tmp_path)
    before = store.capture_before(f"turn-{change_git_state}")
    (repo / "modified.txt").write_text("agent result", encoding="utf-8")
    if change_git_state == "head":
        _git(repo, "commit", "--allow-empty", "-m", "head moved")
    else:
        _git(repo, "add", "modified.txt")

    manifest = store.capture_after(f"turn-{change_git_state}", before)
    assert manifest.applicable is False
    assert "HEAD or index" in manifest.blocked_reason
    with pytest.raises(ChangeSetUnavailableError, match="HEAD or index"):
        store.undo(f"turn-{change_git_state}")


def test_git_index_drift_after_capture_is_a_conflict(repo: Path, tmp_path: Path):
    store = _store(repo, tmp_path)
    before = store.capture_before("turn-index-drift")
    (repo / "modified.txt").write_text("agent result", encoding="utf-8")
    store.capture_after("turn-index-drift", before)

    _git(repo, "add", "modified.txt")
    with pytest.raises(ChangeSetConflictError, match="HEAD or index"):
        store.undo("turn-index-drift")
    assert (repo / "modified.txt").read_text(encoding="utf-8") == "agent result"


def test_capture_limits_leave_no_partial_changeset(repo: Path, tmp_path: Path):
    store = _store(repo, tmp_path, max_file_bytes=3)
    with pytest.raises(ChangeSetLimitError, match="exceeds 3 bytes"):
        store.capture_before("turn-large")
    assert not (tmp_path / "changes" / "turn-large").exists()


def test_before_snapshot_cannot_be_attached_to_another_turn(repo: Path, tmp_path: Path):
    store = _store(repo, tmp_path)
    before = store.capture_before("turn-original")
    with pytest.raises(ChangeSetError, match="another turn or workspace"):
        store.capture_after("turn-other", before)
    assert not (tmp_path / "changes" / "turn-other").exists()


def test_file_count_and_total_size_limits(repo: Path, tmp_path: Path):
    with pytest.raises(ChangeSetLimitError, match="files; limit is 1"):
        _store(repo, tmp_path, max_files=1).capture_before("turn-files")
    with pytest.raises(ChangeSetLimitError, match="snapshot exceeds"):
        _store(repo, tmp_path, max_total_bytes=5).capture_before("turn-bytes")


@pytest.mark.parametrize("turn_id", ["../escape", "/absolute", "bad/name", ""])
def test_turn_id_cannot_escape_changes_directory(repo: Path, tmp_path: Path, turn_id: str):
    store = _store(repo, tmp_path)
    with pytest.raises(ValueError, match="Invalid turn id"):
        store.capture_before(turn_id)


def test_tampered_blob_is_rejected_before_workspace_writes(repo: Path, tmp_path: Path):
    store = _store(repo, tmp_path)
    before = store.capture_before("turn-tamper")
    (repo / "modified.txt").write_text("agent result", encoding="utf-8")
    (repo / "renamed.txt").write_text("second result", encoding="utf-8")
    manifest = store.capture_after("turn-tamper", before)
    first = manifest.files[0]
    blob = tmp_path / "changes" / "turn-tamper" / first["before"]["blob"]
    blob.write_bytes(b"tampered")

    with pytest.raises(ChangeSetError, match="integrity"):
        store.undo("turn-tamper")
    assert (repo / "modified.txt").read_text(encoding="utf-8") == "agent result"
    assert (repo / "renamed.txt").read_text(encoding="utf-8") == "second result"


@pytest.mark.skipif(os.name == "nt", reason="Creating symlinks requires optional Windows privilege")
def test_symlink_target_is_captured_and_restored(repo: Path, tmp_path: Path):
    link = repo / "link.txt"
    os.symlink("modified.txt", link)
    store = _store(repo, tmp_path)
    before = store.capture_before("turn-link")
    link.unlink()
    os.symlink("renamed.txt", link)
    store.capture_after("turn-link", before)

    store.undo("turn-link")
    assert link.is_symlink()
    assert os.readlink(link) == "modified.txt"
    store.reapply("turn-link")
    assert os.readlink(link) == "renamed.txt"
