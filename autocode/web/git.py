"""Git repository inspection and safe mutations for the Web workspace."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

MAX_DIFF_BYTES = 2 * 1024 * 1024
MAX_ACTION_PATHS = 200


class GitCommandError(ValueError):
    """A user-facing Git command failure."""


class GitWorkspace:
    """Expose bounded Git operations for one CLI-registered workspace."""

    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.repo_root = self._discover_repo()

    @classmethod
    def inspect(cls, workspace: Path) -> dict[str, Any]:
        try:
            return cls(workspace).snapshot()
        except GitCommandError as exc:
            return {
                "available": False,
                "message": str(exc),
                "branch": "",
                "changes": [],
                "branches": [],
                "remote_branches": [],
                "additions": 0,
                "deletions": 0,
            }

    def snapshot(self) -> dict[str, Any]:
        branch = self._run(["branch", "--show-current"]).strip()
        head = self._run(["rev-parse", "--short", "HEAD"], allowed={0, 128}).strip()
        detached = not branch and bool(head)
        upstream = self._run(
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
            allowed={0, 128},
        ).strip()
        ahead, behind = self._ahead_behind(upstream)
        changes = self._changes()
        branches, remote_branches = self._branches()
        remotes = [
            item.strip()
            for item in self._run(["remote"]).splitlines()
            if item.strip()
        ]
        gh_path = shutil.which("gh")
        return {
            "available": True,
            "repo_root": str(self.repo_root),
            "branch": branch or (f"detached@{head}" if detached else "未提交"),
            "head": head,
            "detached": detached,
            "upstream": upstream,
            "ahead": ahead,
            "behind": behind,
            "additions": sum(item["additions"] for item in changes),
            "deletions": sum(item["deletions"] for item in changes),
            "changes": changes,
            "branches": branches,
            "remote_branches": remote_branches,
            "default_base": self._default_base(branch, branches, remote_branches),
            "remotes": remotes,
            "gh_available": bool(gh_path),
        }

    def diff(self, *, scope: str, path: str = "", base: str = "") -> dict[str, Any]:
        if scope == "changes":
            snapshot = self.snapshot()
            known_paths = {item["path"] for item in snapshot["changes"]}
            selected_path = (
                self._validate_diff_path(path, known_paths) if path else ""
            )
            args = ["diff", "--no-ext-diff", "--find-renames", "--unified=3"]
            if self._has_head():
                args.append("HEAD")
            if selected_path:
                args.extend(["--", selected_path])
            raw = self._run(args, max_bytes=MAX_DIFF_BYTES)
            untracked = [
                item
                for item in snapshot["changes"]
                if item["status"] == "untracked"
                and (not selected_path or item["path"] == selected_path)
            ]
            raw = self._append_untracked_diffs(raw, untracked)
            files = (
                [item for item in snapshot["changes"] if item["path"] == selected_path]
                if selected_path
                else snapshot["changes"]
            )
            return {
                "scope": scope,
                "base": "",
                "path": selected_path,
                "diff": raw,
                "files": files,
                "truncated": len(raw.encode("utf-8")) >= MAX_DIFF_BYTES,
            }
        if scope == "compare":
            verified_base = self._validate_base(base)
            compare_files = self._compare_files(verified_base)
            selected_path = (
                self._validate_diff_path(
                    path,
                    {item["path"] for item in compare_files},
                )
                if path
                else ""
            )
            args = [
                "diff",
                "--no-ext-diff",
                "--find-renames",
                "--unified=3",
                f"{verified_base}...HEAD",
            ]
            if selected_path:
                args.extend(["--", selected_path])
            raw = self._run(args, max_bytes=MAX_DIFF_BYTES)
            files = compare_files
            if selected_path:
                files = [item for item in files if item["path"] == selected_path]
            return {
                "scope": scope,
                "base": verified_base,
                "path": selected_path,
                "diff": raw,
                "files": files,
                "truncated": len(raw.encode("utf-8")) >= MAX_DIFF_BYTES,
            }
        raise GitCommandError("Unsupported Git diff scope.")

    def action(
        self,
        *,
        action: str,
        paths: list[str] | None = None,
        branch: str = "",
        message: str = "",
    ) -> dict[str, Any]:
        output = ""
        if action in {"stage", "unstage"}:
            safe_paths = self._validate_action_paths(paths or [], action)
            if action == "stage":
                output = self._run(["add", "--", *safe_paths])
            elif self._has_head():
                output = self._run(["restore", "--staged", "--", *safe_paths])
            else:
                output = self._run(["rm", "--cached", "--", *safe_paths])
        elif action == "switch":
            branch_names = {item["name"] for item in self.snapshot()["branches"]}
            if branch not in branch_names:
                raise GitCommandError("Only an existing local branch can be switched.")
            output = self._run(["switch", "--", branch], timeout=30)
        elif action == "create_branch":
            candidate = branch.strip()
            if not candidate or len(candidate) > 200:
                raise GitCommandError(
                    "Branch name is required and must be at most 200 characters."
                )
            self._run(["check-ref-format", "--branch", candidate])
            output = self._run(["switch", "-c", candidate], timeout=30)
        elif action == "commit":
            clean_message = message.strip()
            if not clean_message or len(clean_message) > 500:
                raise GitCommandError(
                    "Commit message is required and must be at most 500 characters."
                )
            if not any(item["staged"] for item in self._changes()):
                raise GitCommandError("There are no staged changes to commit.")
            output = self._run(["commit", "-m", clean_message], timeout=120)
        elif action == "push":
            state = self.snapshot()
            if state["detached"] or state["branch"] == "未提交":
                raise GitCommandError("A named branch is required before pushing.")
            if state["upstream"]:
                output = self._run(["push"], timeout=180)
            elif "origin" in state["remotes"]:
                output = self._run(
                    ["push", "-u", "origin", state["branch"]],
                    timeout=180,
                )
            else:
                raise GitCommandError("No upstream or origin remote is configured.")
        else:
            raise GitCommandError("Unsupported Git action.")
        return {
            "action": action,
            "output": output.strip(),
            "git": self.snapshot(),
        }

    def _discover_repo(self) -> Path:
        result = self._run_external(
            ["git", "rev-parse", "--show-toplevel"],
            allowed={0, 128},
            cwd=self.workspace,
        )
        if result[0] != 0 or not result[1].strip():
            raise GitCommandError("当前 workspace 不是 Git 仓库。")
        root = Path(result[1].strip()).resolve()
        if root != self.workspace:
            raise GitCommandError("请在 CLI 中注册 Git 仓库根目录作为 workspace。")
        return root

    def _changes(self) -> list[dict[str, Any]]:
        raw = self._run_bytes(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        )
        records = raw.split(b"\0")
        parsed: list[dict[str, Any]] = []
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if not record:
                continue
            text = record.decode("utf-8", errors="replace")
            if len(text) < 4:
                continue
            x, y, path = text[0], text[1], text[3:]
            old_path = ""
            if x in {"R", "C"} and index < len(records):
                old_path = records[index].decode("utf-8", errors="replace")
                index += 1
            normalized = path.replace("\\", "/")
            status = self._status_name(x, y)
            parsed.append(
                {
                    "path": normalized,
                    "old_path": old_path.replace("\\", "/"),
                    "index_status": x,
                    "worktree_status": y,
                    "status": status,
                    "staged": x not in {" ", "?"},
                    "unstaged": y not in {" "},
                    "additions": 0,
                    "deletions": 0,
                }
            )
        tracked_stats = self._tracked_stats()
        for item in parsed:
            if item["status"] == "untracked":
                item["additions"], item["deletions"] = self._untracked_stats(
                    item["path"]
                )
            else:
                item["additions"], item["deletions"] = tracked_stats.get(
                    item["path"],
                    (0, 0),
                )
        return parsed

    def _tracked_stats(self) -> dict[str, tuple[int, int]]:
        args = ["diff", "--numstat"]
        if self._has_head():
            args.append("HEAD")
        output = self._run(args, allowed={0, 128})
        stats: dict[str, tuple[int, int]] = {}
        for line in output.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
                continue
            stats[parts[2].replace("\\", "/")] = (int(parts[0]), int(parts[1]))
        return stats

    def _untracked_stats(self, path: str) -> tuple[int, int]:
        target = (self.repo_root / path).resolve()
        try:
            target.relative_to(self.repo_root)
            if target.stat().st_size > 1024 * 1024:
                return 0, 0
            data = target.read_bytes()
            if b"\0" in data:
                return 0, 0
            return len(data.splitlines()) or (1 if data else 0), 0
        except (OSError, ValueError):
            return 0, 0

    def _branches(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        output = self._run(
            [
                "for-each-ref",
                "--sort=-committerdate",
                "--format=%(refname:short)\t%(refname)\t%(HEAD)\t%(objectname:short)",
                "refs/heads",
                "refs/remotes",
            ]
        )
        local: list[dict[str, Any]] = []
        remote: list[dict[str, Any]] = []
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) != 4:
                continue
            name, ref, head_marker, commit = parts
            item = {
                "name": name,
                "ref": ref,
                "current": head_marker == "*",
                "commit": commit,
            }
            if ref.startswith("refs/heads/"):
                local.append(item)
            elif not name.endswith("/HEAD"):
                remote.append(item)
        return local, remote

    def _ahead_behind(self, upstream: str) -> tuple[int, int]:
        if not upstream:
            return 0, 0
        output = self._run(
            ["rev-list", "--left-right", "--count", f"HEAD...{upstream}"],
            allowed={0, 128},
        ).strip()
        parts = output.split()
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            return 0, 0
        return int(parts[0]), int(parts[1])

    @staticmethod
    def _default_base(
        current: str,
        local: list[dict[str, Any]],
        remote: list[dict[str, Any]],
    ) -> str:
        local_names = [item["name"] for item in local if item["name"] != current]
        remote_names = [item["name"] for item in remote]
        for candidate in ("main", "master", "origin/main", "origin/master"):
            if candidate in local_names or candidate in remote_names:
                return candidate
        return (local_names + remote_names + [""])[0]

    def _validate_diff_path(self, path: str, allowed_paths: set[str]) -> str:
        normalized = path.strip().replace("\\", "/")
        if not normalized or len(normalized) > 1000:
            raise GitCommandError("Invalid Git path.")
        target = (self.repo_root / normalized).resolve()
        try:
            target.relative_to(self.repo_root)
        except ValueError as exc:
            raise GitCommandError("Git path must stay inside the workspace.") from exc
        if normalized not in allowed_paths:
            raise GitCommandError("Git path is not part of the selected changes.")
        return normalized

    def _validate_action_paths(self, paths: list[str], action: str) -> list[str]:
        if not paths or len(paths) > MAX_ACTION_PATHS:
            raise GitCommandError("Select between 1 and 200 changed files.")
        changes = self._changes()
        allowed = {
            item["path"]
            for item in changes
            if action == "stage" or item["staged"]
        }
        normalized: list[str] = []
        for path in paths:
            value = path.strip().replace("\\", "/")
            if value not in allowed:
                raise GitCommandError("Only current workspace changes can be staged or unstaged.")
            normalized.append(value)
        return list(dict.fromkeys(normalized))

    def _validate_base(self, base: str) -> str:
        candidate = base.strip()
        if candidate not in self._all_branch_names():
            raise GitCommandError("Compare base must be an existing local or remote branch.")
        self._run(["rev-parse", "--verify", f"{candidate}^{{commit}}"])
        return candidate

    def _all_branch_names(self) -> set[str]:
        local, remote = self._branches()
        return {item["name"] for item in [*local, *remote]}

    def _compare_files(self, base: str) -> list[dict[str, Any]]:
        output = self._run(["diff", "--name-status", "-z", f"{base}...HEAD"])
        records = output.split("\0")
        files: list[dict[str, Any]] = []
        index = 0
        while index < len(records):
            status = records[index]
            index += 1
            if not status or index >= len(records):
                continue
            path = records[index].replace("\\", "/")
            index += 1
            old_path = ""
            if status.startswith(("R", "C")) and index < len(records):
                old_path, path = path, records[index].replace("\\", "/")
                index += 1
            files.append(
                {
                    "path": path,
                    "old_path": old_path,
                    "status": self._name_status(status[:1]),
                    "staged": False,
                    "unstaged": False,
                    "additions": 0,
                    "deletions": 0,
                }
            )
        return files

    def _append_untracked_diffs(self, raw: str, files: list[dict[str, Any]]) -> str:
        chunks = [raw.rstrip()]
        for item in files:
            path = item["path"]
            target = (self.repo_root / path).resolve()
            try:
                data = target.read_bytes()
            except OSError:
                continue
            if b"\0" in data:
                chunks.append(f"diff --git a/{path} b/{path}\nBinary file {path} added")
                continue
            lines = data.decode("utf-8", errors="replace").splitlines()
            diff_lines = [
                f"diff --git a/{path} b/{path}",
                "new file mode 100644",
                "--- /dev/null",
                f"+++ b/{path}",
                f"@@ -0,0 +1,{len(lines)} @@",
                *[f"+{line}" for line in lines],
            ]
            chunks.append("\n".join(diff_lines))
            if len("\n".join(chunks).encode("utf-8")) >= MAX_DIFF_BYTES:
                break
        return "\n".join(chunk for chunk in chunks if chunk)[:MAX_DIFF_BYTES]

    def _has_head(self) -> bool:
        return self._run(["rev-parse", "--verify", "HEAD"], allowed={0, 128}).strip() != ""

    @staticmethod
    def _status_name(x: str, y: str) -> str:
        if x == "?" and y == "?":
            return "untracked"
        if "U" in {x, y} or (x, y) in {("A", "A"), ("D", "D")}:
            return "conflict"
        code = y if y != " " else x
        return GitWorkspace._name_status(code)

    @staticmethod
    def _name_status(code: str) -> str:
        return {
            "A": "added",
            "C": "copied",
            "D": "deleted",
            "M": "modified",
            "R": "renamed",
            "T": "type_changed",
        }.get(code, "modified")

    def _run(
        self,
        args: list[str],
        *,
        allowed: set[int] | None = None,
        timeout: int = 15,
        max_bytes: int = MAX_DIFF_BYTES,
    ) -> str:
        code, stdout, stderr = self._run_external(
            ["git", *args],
            allowed=allowed or {0},
            cwd=self.repo_root,
            timeout=timeout,
            max_bytes=max_bytes,
        )
        if code not in (allowed or {0}):
            raise GitCommandError(stderr.strip() or "Git command failed.")
        return stdout

    def _run_bytes(self, args: list[str]) -> bytes:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=self.repo_root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitCommandError(f"Git command could not run: {exc}") from exc
        if completed.returncode != 0:
            message = completed.stderr.decode("utf-8", errors="replace").strip()
            raise GitCommandError(message or "Git command failed.")
        return completed.stdout

    @staticmethod
    def _run_external(
        command: list[str],
        *,
        allowed: set[int],
        cwd: Path | None = None,
        timeout: int = 15,
        max_bytes: int = MAX_DIFF_BYTES,
    ) -> tuple[int, str, str]:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_OPTIONAL_LOCKS"] = "0"
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise GitCommandError("Git is not installed on the local computer.") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitCommandError("Git command timed out.") from exc
        stdout = completed.stdout[:max_bytes].decode("utf-8", errors="replace")
        stderr = completed.stderr[:max_bytes].decode("utf-8", errors="replace")
        if completed.returncode not in allowed:
            raise GitCommandError(stderr.strip() or "Git command failed.")
        return completed.returncode, stdout, stderr
