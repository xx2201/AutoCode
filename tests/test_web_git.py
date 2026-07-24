import subprocess

import pytest

from autocode.web.git import GitCommandError, GitWorkspace


def _git(repo, *args):
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _repo(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Web Git Test")
    _git(repo, "config", "user.email", "web-git@example.test")
    tracked = repo / "app.py"
    tracked.write_text("print('one')\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "initial")
    return repo


def test_git_snapshot_and_diff_include_tracked_and_untracked_changes(tmp_path):
    repo = _repo(tmp_path)
    (repo / "app.py").write_text("print('one')\nprint('two')\n", encoding="utf-8")
    (repo / "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8")

    state = GitWorkspace(repo).snapshot()
    diff = GitWorkspace(repo).diff(scope="changes")

    assert state["available"] is True
    assert state["branch"] == "main"
    assert {item["path"] for item in state["changes"]} == {"app.py", "notes.txt"}
    assert state["additions"] == 3
    assert "diff --git a/app.py b/app.py" in diff["diff"]
    assert "diff --git a/notes.txt b/notes.txt" in diff["diff"]
    assert "+beta" in diff["diff"]


def test_git_stage_unstage_commit_and_branch_compare(tmp_path):
    repo = _repo(tmp_path)
    workspace = GitWorkspace(repo)
    (repo / "app.py").write_text("print('changed')\n", encoding="utf-8")

    staged = workspace.action(action="stage", paths=["app.py"])
    assert staged["git"]["changes"][0]["staged"] is True

    unstaged = workspace.action(action="unstage", paths=["app.py"])
    assert unstaged["git"]["changes"][0]["staged"] is False

    workspace.action(action="stage", paths=["app.py"])
    committed = workspace.action(action="commit", message="change app")
    assert committed["git"]["changes"] == []

    created = workspace.action(action="create_branch", branch="feature/review")
    assert created["git"]["branch"] == "feature/review"
    (repo / "feature.txt").write_text("review me\n", encoding="utf-8")
    workspace.action(action="stage", paths=["feature.txt"])
    workspace.action(action="commit", message="add feature")

    compared = workspace.diff(scope="compare", base="main")
    assert [item["path"] for item in compared["files"]] == ["feature.txt"]
    assert "+review me" in compared["diff"]

    switched = workspace.action(action="switch", branch="main")
    assert switched["git"]["branch"] == "main"


def test_git_push_sets_origin_upstream(tmp_path):
    repo = _repo(tmp_path)
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare")
    _git(repo, "remote", "add", "origin", str(remote))

    result = GitWorkspace(repo).action(action="push")

    assert result["git"]["upstream"] == "origin/main"
    assert _git(remote, "rev-parse", "--verify", "refs/heads/main")


def test_git_rejects_paths_and_branches_outside_current_state(tmp_path):
    repo = _repo(tmp_path)
    workspace = GitWorkspace(repo)
    (repo / "app.py").write_text("changed\n", encoding="utf-8")

    with pytest.raises(GitCommandError, match="current workspace changes"):
        workspace.action(action="stage", paths=["../outside.txt"])
    with pytest.raises(GitCommandError, match="existing local branch"):
        workspace.action(action="switch", branch="missing")
    with pytest.raises(GitCommandError):
        workspace.action(action="create_branch", branch="bad..branch")
    with pytest.raises(GitCommandError, match="Compare base"):
        workspace.diff(scope="compare", base="../../etc")


def test_git_inspect_reports_non_repository_without_throwing(tmp_path):
    result = GitWorkspace.inspect(tmp_path)

    assert result["available"] is False
    assert "不是 Git 仓库" in result["message"]


def test_git_inspect_requires_registered_workspace_to_be_repository_root(tmp_path):
    repo = _repo(tmp_path)
    nested = repo / "src"
    nested.mkdir()

    result = GitWorkspace.inspect(nested)

    assert result["available"] is False
    assert "仓库根目录" in result["message"]
