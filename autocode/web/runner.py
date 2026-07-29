"""Local AutoCode runner that connects outward to the Web relay."""

from __future__ import annotations

import os
import logging
import queue
import signal
import ssl
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values

from .. import __version__
from ..config import Config
from ..llm import api_format_for_provider
from ..diagnostics import diagnostic_log_dir, get_diagnostic_logger, log_event
from ..mcp import get_shared_mcp_manager
from ..remote.manager import RemoteManager, presentation_tool_arguments
from ..state.changes import (
    ChangeSetConflictError,
    ChangeSetStore,
    ChangeSetUnavailableError,
)
from ..state.checkpoint import migrate_session_storage
from ..tools.factory import build_agent_tools
from ..workspaces import WorkspaceRegistry
from .files import WebFileStore, WebSendTool, WorkspaceFileBrowser
from .git import GitWorkspace


_GIT_CHANGE_FIELDS = (
    "old_path",
    "status",
    "index_status",
    "worktree_status",
    "staged",
    "unstaged",
    "additions",
    "deletions",
    "_worktree_fingerprint",
)


def git_turn_snapshot(workspace: Path) -> dict:
    """Add cheap local file identities to a Git snapshot for turn-level comparison."""
    snapshot = GitWorkspace.inspect(workspace)
    if not snapshot.get("available"):
        return snapshot
    repo_root = Path(str(snapshot["repo_root"])).resolve()
    for item in snapshot.get("changes", []):
        target = (repo_root / str(item.get("path", ""))).resolve()
        try:
            target.relative_to(repo_root)
            stat = target.stat()
            item["_worktree_fingerprint"] = (stat.st_size, stat.st_mtime_ns)
        except (OSError, ValueError):
            item["_worktree_fingerprint"] = None
    return snapshot


def changed_git_files(before: dict, after: dict) -> list[dict]:
    """Return files whose observable Git state changed during one Agent turn."""
    if not before.get("available") or not after.get("available"):
        return []
    before_by_path = {
        str(item.get("path", "")): item
        for item in before.get("changes", [])
        if item.get("path")
    }
    changed = []
    for item in after.get("changes", []):
        path = str(item.get("path", ""))
        if not path:
            continue
        previous = before_by_path.get(path)
        before_signature = (
            tuple(previous.get(field) for field in _GIT_CHANGE_FIELDS)
            if previous
            else None
        )
        after_signature = tuple(item.get(field) for field in _GIT_CHANGE_FIELDS)
        if before_signature == after_signature:
            continue
        changed.append(
            {
                "path": path,
                "status": str(item.get("status", "modified")),
                "additions": max(0, int(item.get("additions", 0) or 0)),
                "deletions": max(0, int(item.get("deletions", 0) or 0)),
            }
        )
    return changed


@dataclass(frozen=True)
class RunnerSettings:
    relay_url: str
    token: str
    ca_cert: str
    poll_wait: float = 25.0


def _setting(values: dict[str, str | None], name: str, default: str = "") -> str:
    return os.getenv(name) or values.get(name) or default


def load_runner_settings(env_file: str | None = None) -> RunnerSettings:
    path = Path(
        env_file
        or os.getenv("AUTOCODE_RUNNER_ENV_FILE")
        or Path.home() / ".autocode" / "web-runner.env"
    ).expanduser()
    values = dotenv_values(path) if path.exists() else {}
    relay_url = _setting(values, "AUTOCODE_RELAY_URL").rstrip("/")
    token = _setting(values, "AUTOCODE_RUNNER_TOKEN")
    ca_cert = _setting(values, "AUTOCODE_RELAY_CA_CERT")
    poll_wait = float(_setting(values, "AUTOCODE_RUNNER_POLL_WAIT", "25"))

    if not relay_url.startswith("https://"):
        raise RuntimeError("AUTOCODE_RELAY_URL must use HTTPS.")
    if len(token) < 24:
        raise RuntimeError("AUTOCODE_RUNNER_TOKEN must contain at least 24 characters.")
    if not ca_cert or not Path(ca_cert).expanduser().is_file():
        raise RuntimeError("AUTOCODE_RELAY_CA_CERT must point to the relay CA certificate.")
    return RunnerSettings(
        relay_url=relay_url,
        token=token,
        ca_cert=str(Path(ca_cert).expanduser().resolve()),
        poll_wait=max(1.0, min(poll_wait, 30.0)),
    )


class LocalRunner:
    """Execute relayed actions against one local RemoteManager."""

    def __init__(
        self,
        settings: RunnerSettings,
        *,
        config: Config | None = None,
        registry: WorkspaceRegistry | None = None,
        manager_factory: Any | None = None,
        client: httpx.Client | Any | None = None,
    ):
        self.settings = settings
        self.registry = registry or WorkspaceRegistry()
        self._base_config = config or Config.from_env()
        if not self._base_config.model:
            raise RuntimeError("AUTOCODE_MODEL is required in the Agent runtime .env.")
        if not self._base_config.api_key:
            raise RuntimeError("AUTOCODE_API_KEY is required in the Agent runtime .env.")
        self._manager_factory = manager_factory or self._build_manager
        self._managers: dict[str, RemoteManager | Any] = {}
        self._manager_lock = threading.Lock()
        self._workspace_locks_guard = threading.Lock()
        self._workspace_locks: dict[str, threading.RLock] = {}
        self._pending_changes: dict[tuple[str, str], tuple[ChangeSetStore, Any, str]] = {}
        self._owns_client = client is None
        if client is None:
            ssl_context = ssl.create_default_context(cafile=settings.ca_cert)
            timeout = httpx.Timeout(
                connect=10.0,
                read=settings.poll_wait + 15.0,
                write=30.0,
                pool=10.0,
            )
            client = httpx.Client(
                base_url=settings.relay_url,
                headers={
                    "Authorization": f"Bearer {settings.token}",
                    "User-Agent": f"AutoCode-Local-Runner/{__version__}",
                },
                verify=ssl_context,
                timeout=timeout,
            )
        self.client = client
        self._stopping = False
        self._logger = get_diagnostic_logger("web-runner")
        self._file_context = threading.local()
        self._web_files = WebFileStore()
        self._job_executor = ThreadPoolExecutor(
            max_workers=8,
            thread_name_prefix="autocode-runner-job",
        )

    def execute(self, action: str, payload: dict[str, Any], event_handler=None) -> Any:
        if action == "bootstrap":
            return {
                "model": self._base_config.model,
                "provider": self._base_config.provider,
                "api_format": api_format_for_provider(self._base_config.provider),
                "context_window_tokens": self._base_config.max_context_tokens,
                "workspaces": self.registry.list_workspaces(),
                "version": __version__,
                "capabilities": {
                    "streaming": True,
                    "file_upload": True,
                    "file_download": True,
                    "git_workspace": True,
                    "image_input": True,
                    "workspace_image_tool": True,
                    "web_search": bool(self._base_config.tavily_api_key),
                    "permission_modes": ["ask", "full_access"],
                },
                "diagnostics": {
                    "log_dir": str(diagnostic_log_dir()),
                    "langfuse_configured": bool(
                        self._base_config.langfuse_public_key
                        and self._base_config.langfuse_secret_key
                    ),
                },
            }
        workspace_id = str(payload["workspace_id"])
        if action == "git_status":
            workspace = self.registry.resolve(workspace_id)
            return GitWorkspace.inspect(workspace)
        if action == "git_diff":
            workspace = self.registry.resolve(workspace_id)
            return GitWorkspace(workspace).diff(
                scope=str(payload["scope"]),
                path=str(payload.get("path", "")),
                base=str(payload.get("base", "")),
            )
        if action == "git_action":
            workspace = self.registry.resolve(workspace_id)
            return GitWorkspace(workspace).action(
                action=str(payload["git_action"]),
                paths=list(payload.get("paths") or []),
                branch=str(payload.get("branch", "")),
                message=str(payload.get("message", "")),
            )
        if action == "workspace_files":
            workspace = self.registry.resolve(workspace_id)
            return WorkspaceFileBrowser(workspace).list_files()
        if action == "workspace_file":
            workspace = self.registry.resolve(workspace_id)
            return WorkspaceFileBrowser(workspace).read(str(payload["path"]))
        manager = self._manager(str(payload["workspace_id"]))
        if action == "diagnostics":
            return {
                "summary": {
                    "log_dir": str(diagnostic_log_dir()),
                    "observability": manager.observability_status(),
                }
        }
        if action == "chat":
            workspace = self.registry.resolve(workspace_id)
            return self._execute_agent_turn(
                manager,
                workspace,
                payload,
                event_handler=event_handler,
            )
        if action == "edit_turn":
            workspace = self.registry.resolve(workspace_id)
            return self._execute_agent_turn(
                manager,
                workspace,
                payload,
                event_handler=event_handler,
                edit_turn_id=str(payload.get("turn_id", "")),
            )
        if action == "turn_message":
            expected_turn_id = str(payload.get("expected_turn_id", ""))
            mode = str(payload.get("mode", ""))
            prompt = str(payload.get("prompt", ""))
            if mode == "steer":
                item = manager.steer(payload["client_id"], expected_turn_id, prompt)
            elif mode == "queue":
                item = manager.enqueue_followup(
                    payload["client_id"], expected_turn_id, prompt
                )
            else:
                raise ValueError("Unsupported turn message mode.")
            queued_message = {
                "id": item.get("message_id", ""),
                "prompt": item.get("content", ""),
                "created_at": item.get("created_at", ""),
            }
            return {
                "accepted": True,
                "mode": mode,
                "turn_id": expected_turn_id,
                "queued_message": queued_message,
            }
        if action == "permission_mode":
            return {
                "permission_mode": manager.set_permission_mode(
                    payload["client_id"],
                    str(payload.get("permission_mode", "")),
                )
            }
        if action == "change_action":
            workspace = self.registry.resolve(workspace_id)
            turn_id = str(payload.get("turn_id", ""))
            session_id = manager.current_session_id(payload["client_id"])
            store = ChangeSetStore(workspace, session_id)
            try:
                if payload.get("change_action") == "undo":
                    manifest = store.undo(turn_id)
                elif payload.get("change_action") == "reapply":
                    manifest = store.reapply(turn_id)
                else:
                    raise ValueError("Unsupported ChangeSet action.")
            except (ChangeSetConflictError, ChangeSetUnavailableError) as exc:
                raise ValueError(str(exc)) from exc
            return {
                "turn_id": turn_id,
                "state": manifest.state,
                "changed_paths": list(manifest.changed_paths),
                "can_undo": manifest.state == "applied" and manifest.applicable,
                "can_reapply": manifest.state == "undone" and manifest.applicable,
                "blocked_reason": manifest.blocked_reason,
                "git": GitWorkspace.inspect(workspace),
            }
        if action == "approval_decision":
            return manager.decide_approval(
                payload["client_id"],
                payload["approval_id"],
                payload["approval_action"],
                payload["expected_turn_id"],
                payload["batch_id"],
            )
        if action == "continue_turn":
            workspace = self.registry.resolve(workspace_id)
            git_before = git_turn_snapshot(workspace)
            first_token_seen = False

            def on_token(text: str) -> None:
                nonlocal first_token_seen
                if event_handler is None:
                    return
                if not first_token_seen:
                    event_handler({"type": "stage", "stage": "first_token"})
                    first_token_seen = True
                event_handler({"type": "token", "text": text})

            def on_hook(event: str, data: dict) -> None:
                if event_handler is None:
                    return
                self._emit_hook_event(event_handler, event, data)

            with self._capture_output_files(str(payload["workspace_id"])) as output_files:
                result = manager.continue_approval_batch(
                    payload["client_id"],
                    payload["expected_turn_id"],
                    payload["batch_id"],
                    hook_handler=on_hook,
                    on_token=on_token,
                )
            changed_files = changed_git_files(
                git_before,
                git_turn_snapshot(workspace),
            )
            change_key = (workspace_id, str(payload["client_id"]))
            pending_change = self._pending_changes.get(change_key)
            if pending_change is not None and not result.pending_tool:
                store, before, turn_id = self._pending_changes.pop(change_key)
                manifest = store.capture_after(turn_id, before)
                changed_files = self._decorate_changeset_files(changed_files, manifest)
            manager.annotate_turn_changes(payload["client_id"], changed_files)
            response = asdict(result)
            response["files"] = output_files
            response["changed_files"] = changed_files
            return response
        if action == "download":
            return self._web_files.read(
                str(payload["workspace_id"]),
                str(payload["file_id"]),
            )
        if action == "sessions":
            return {"sessions": manager.list_resume_candidates(payload.get("limit", 50))}
        if action == "resume":
            result = manager.resume_session(
                payload["client_id"],
                payload["session_id"],
                payload.get("permission_mode"),
            )
            return {
                "result": asdict(result),
                "messages": manager.conversation_messages(payload["client_id"]),
            }
        if action == "delete_session":
            manager.delete_session(payload["session_id"])
            return {"deleted": True, "session_id": payload["session_id"]}
        if action == "task":
            try:
                summary = manager.current_task_summary(payload["client_id"])
            except ValueError:
                summary = "No active web session."
            return {"summary": summary}
        if action == "trace":
            return {"summary": manager.current_trace(payload["client_id"])}
        if action == "messages":
            try:
                items = manager.conversation_messages(payload["client_id"])
            except ValueError:
                items = []
            return {"messages": items}
        if action == "reset":
            manager.reset_chat(payload["client_id"])
            return {"reset": True}
        raise ValueError(f"Unknown relay action: {action}")

    def _execute_agent_turn(
        self,
        manager,
        workspace: Path,
        payload: dict[str, Any],
        *,
        event_handler=None,
        edit_turn_id: str = "",
    ) -> dict[str, Any]:
        """Run one visible turn and any FIFO follow-ups on the same event stream."""
        client_id = payload["client_id"]
        prompt = str(payload.get("prompt", ""))
        attachments = list(payload.get("attachments") or [])
        queued_turn = False
        all_files: list[dict] = []
        all_changed: list[dict] = []
        result = None

        while True:
            git_before = git_turn_snapshot(workspace)
            first_token_seen = False
            change_store = None
            change_before = None
            started_turn_id = ""

            def on_token(text: str) -> None:
                nonlocal first_token_seen
                if event_handler is None:
                    return
                if not first_token_seen:
                    event_handler({"type": "stage", "stage": "first_token"})
                    first_token_seen = True
                event_handler({"type": "token", "text": text})

            def on_hook(event: str, data: dict) -> None:
                nonlocal change_store, change_before, started_turn_id
                if event == "turn_started" and git_before.get("available"):
                    started_turn_id = str(data.get("task_id", ""))
                    change_store = ChangeSetStore(
                        workspace,
                        str(data.get("session_id", "")),
                    )
                    change_before = change_store.capture_before(started_turn_id)
                if event_handler is None:
                    return
                if event == "turn_started":
                    event_handler(
                        {
                            "type": "turn",
                            "phase": "started",
                            "turn_id": str(data.get("task_id", "")),
                            "revision_id": str(data.get("revision_id", "")),
                            "queued": queued_turn,
                        }
                    )
                self._emit_hook_event(event_handler, event, data)

            submit_kwargs = {"hook_handler": on_hook}
            submit_kwargs["permission_mode"] = payload.get("permission_mode")
            if event_handler is not None:
                submit_kwargs["on_token"] = on_token
            if attachments:
                submit_kwargs["attachments"] = attachments
            with self._capture_output_files(str(payload["workspace_id"])) as output_files:
                if edit_turn_id:
                    result = manager.edit_last_turn(
                        client_id,
                        edit_turn_id,
                        prompt,
                        **submit_kwargs,
                    )
                    edit_turn_id = ""
                else:
                    result = manager.submit(client_id, prompt, **submit_kwargs)

            changed_files = changed_git_files(
                git_before,
                git_turn_snapshot(workspace),
            )
            if change_store is not None and change_before is not None:
                change_key = (str(payload["workspace_id"]), str(client_id))
                if result.pending_tool:
                    self._pending_changes[change_key] = (
                        change_store,
                        change_before,
                        started_turn_id,
                    )
                else:
                    manifest = change_store.capture_after(started_turn_id, change_before)
                    changed_files = self._decorate_changeset_files(changed_files, manifest)
            manager.annotate_turn_changes(client_id, changed_files)
            all_files.extend(output_files)
            all_changed.extend(changed_files)
            if event_handler is not None:
                if first_token_seen:
                    event_handler({"type": "stage", "stage": "last_token"})
                event_handler({"type": "stage", "stage": "persisted"})

            if result.status != "completed":
                break
            pop_queued = getattr(manager, "pop_queued_followup", None)
            followup = pop_queued(client_id) if pop_queued is not None else None
            if followup is None:
                break
            prompt = followup.content
            attachments = []
            queued_turn = True
            if event_handler is not None:
                event_handler(
                    {
                        "type": "turn",
                        "phase": "queued_starting",
                        "message_id": followup.message_id,
                    }
                )

        response = asdict(result)
        response["files"] = all_files
        response["changed_files"] = all_changed
        return response

    @staticmethod
    def _decorate_changeset_files(changed_files: list[dict], manifest) -> list[dict]:
        """Attach persisted undo state and include changes Git presentation missed."""
        known_paths = {item["path"] for item in changed_files}
        for changed_path in manifest.changed_paths:
            if changed_path not in known_paths:
                changed_files.append(
                    {
                        "path": changed_path,
                        "status": "modified",
                        "additions": 0,
                        "deletions": 0,
                    }
                )
        for item in changed_files:
            item.update(
                {
                    "turn_id": manifest.turn_id,
                    "state": manifest.state,
                    "can_undo": bool(manifest.files) and manifest.applicable,
                    "can_reapply": False,
                    "blocked_reason": manifest.blocked_reason,
                }
            )
        return changed_files

    @staticmethod
    def _emit_hook_event(event_handler, event: str, data: dict) -> None:
        if event == "assistant_step":
            tool_calls = data.get("tool_calls") or []
            if tool_calls:
                content = str(data.get("content", ""))
                step_index = int(data.get("step_index", 0) or 0)
                if content:
                    event_handler(
                        {
                            "type": "work",
                            "phase": "narrative",
                            "work_id": f"step-{step_index}-narrative",
                            "content": content,
                        }
                    )
                for tool_call in tool_calls:
                    event_handler(
                        {
                            "type": "work",
                            "phase": "planned",
                            "tool_call_id": str(tool_call.get("id", "")),
                            "tool_name": str(tool_call.get("name", "")),
                            "arguments": presentation_tool_arguments(
                                tool_call.get("arguments")
                            ),
                        }
                    )
        if event in {"before_tool", "after_tool"}:
            work_event = {
                "type": "work",
                "phase": "started" if event == "before_tool" else "completed",
                "tool_call_id": str(data.get("tool_call_id", "")),
                "tool_name": str(data.get("tool_name", "")),
            }
            work_event["arguments"] = presentation_tool_arguments(
                data.get("arguments")
            )
            if event == "after_tool":
                work_event.update(
                    {
                        "output": str(data.get("result", "")),
                        "duration_ms": float(data.get("duration_ms", 0) or 0),
                        "success": bool(data.get("success", False)),
                    }
                )
            event_handler(work_event)
        stage_map = {
            "before_llm": "model_started",
            "after_llm": "model_finished",
            "before_tool": "tool_started",
            "after_tool": "tool_finished",
            "context_compaction": "context_compacted",
            "turn_started": "turn_started",
        }
        stage = stage_map.get(event)
        if stage:
            details = {
                key: data[key]
                for key in (
                    "step_index",
                    "tool_call_id",
                    "tool_name",
                    "prompt_tokens",
                    "completion_tokens",
                    "task_id",
                    "turn_id",
                    "revision_id",
                )
                if key in data
            }
            event_handler({"type": "stage", "stage": stage, "details": details})

    def run_forever(self) -> None:
        retry_delay = 1.0
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            name="autocode-runner-heartbeat",
            daemon=True,
        )
        heartbeat.start()
        try:
            while not self._stopping:
                try:
                    response = self.client.get(
                        "/api/runner/next",
                        params={"wait": self.settings.poll_wait},
                    )
                    if response.status_code == 204:
                        retry_delay = 1.0
                        continue
                    response.raise_for_status()
                    job = response.json()
                    self._job_executor.submit(self._run_job, job)
                    retry_delay = 1.0
                except (httpx.HTTPError, ValueError) as exc:
                    if self._stopping:
                        break
                    print(f"Runner relay error: {exc}", file=sys.stderr, flush=True)
                    time.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 30.0)
        finally:
            self._stopping = True
            heartbeat.join(timeout=2.0)
            self._job_executor.shutdown(wait=True, cancel_futures=False)

    def stop(self) -> None:
        self._stopping = True

    def close(self) -> None:
        self._job_executor.shutdown(wait=True, cancel_futures=False)
        if self._owns_client:
            self.client.close()
        with self._manager_lock:
            managers = list(self._managers.values())
            self._managers.clear()
        for manager in managers:
            manager.close()

    def _manager(self, workspace_id: str):
        with self._manager_lock:
            manager = self._managers.get(workspace_id)
            if manager is not None:
                return manager
            workspace = self.registry.resolve(workspace_id)
            manager = self._manager_factory(workspace)
            self._managers[workspace_id] = manager
            return manager

    def _workspace_lock(self, workspace_id: str) -> threading.RLock:
        with self._workspace_locks_guard:
            return self._workspace_locks.setdefault(workspace_id, threading.RLock())

    def _build_manager(self, workspace: Path) -> RemoteManager:
        config = replace(self._base_config, workspace_root=str(workspace))
        mcp_manager = get_shared_mcp_manager(
            config.workspace_root,
            config.mcp_config_path,
        )
        return RemoteManager(
            config,
            tool_factory=lambda: build_agent_tools(
                config,
                extra_tools=[
                    WebSendTool(
                        lambda file_path: self._offer_web_file(workspace, file_path)
                    )
                ],
                mcp_manager=mcp_manager,
            ),
        )

    @contextmanager
    def _capture_output_files(self, workspace_id: str):
        previous_workspace_id = getattr(self._file_context, "workspace_id", None)
        previous_files = getattr(self._file_context, "files", None)
        files: list[dict] = []
        self._file_context.workspace_id = workspace_id
        self._file_context.files = files
        try:
            yield files
        finally:
            self._file_context.workspace_id = previous_workspace_id
            self._file_context.files = previous_files

    def _offer_web_file(self, workspace: Path, file_path: str) -> str:
        workspace_id = getattr(self._file_context, "workspace_id", None)
        files = getattr(self._file_context, "files", None)
        if not workspace_id or files is None:
            raise RuntimeError("web_send is only available during an active Web turn.")
        metadata = self._web_files.offer(workspace_id, workspace, file_path)
        files.append(metadata)
        return f"Attached {metadata['name']} to the current Web response."

    def _run_job(self, job: dict[str, Any]) -> None:
        job_id = str(job["job_id"])
        action = str(job.get("action", ""))
        payload = dict(job.get("payload") or {})
        started_at = time.monotonic()
        publisher = (
            _RunnerEventPublisher(self.client, job_id, self._logger)
            if bool(job.get("stream"))
            else None
        )

        def emit(event: dict[str, Any]) -> None:
            elapsed_ms = round((time.monotonic() - started_at) * 1000, 1)
            enriched = {**event, "elapsed_ms": event.get("elapsed_ms", elapsed_ms)}
            if publisher is not None:
                publisher.emit(enriched)
            if enriched.get("type") == "stage":
                log_event(
                    self._logger,
                    logging.INFO,
                    "Runner job stage",
                    job_id=job_id,
                    action=action,
                    stage=str(enriched.get("stage", "")),
                    elapsed_ms=enriched["elapsed_ms"],
                    details=enriched.get("details") or {},
                )

        try:
            emit({"type": "stage", "stage": "runner_started"})
            workspace_id = str(payload.get("workspace_id", ""))
            serialized_actions = {
                "chat",
                "edit_turn",
                "continue_turn",
                "change_action",
                "git_action",
                "reset",
                "delete_session",
            }
            lock = (
                self._workspace_lock(workspace_id)
                if workspace_id and action in serialized_actions
                else None
            )
            if lock is None:
                result = self.execute(
                    action,
                    payload,
                    event_handler=emit if publisher is not None else None,
                )
            else:
                with lock:
                    result = self.execute(
                        action,
                        payload,
                        event_handler=emit if publisher is not None else None,
                    )
            emit({"type": "stage", "stage": "runner_completed"})
            body = {"success": True, "result": result}
        except ValueError as exc:
            body = {"success": False, "error": str(exc), "status_code": 409}
        except Exception as exc:
            body = {
                "success": False,
                "error": f"{type(exc).__name__}: {exc}",
                "status_code": 500,
            }

        if publisher is not None:
            publisher.close()
        log_event(
            self._logger,
            logging.INFO if body["success"] else logging.ERROR,
            "Runner job completed",
            job_id=job_id,
            action=action,
            success=body["success"],
            elapsed_ms=round((time.monotonic() - started_at) * 1000, 1),
            error=body.get("error", ""),
        )
        while not self._stopping:
            try:
                response = self.client.post(f"/api/runner/result/{job_id}", json=body)
                if response.status_code == 404:
                    return
                response.raise_for_status()
                return
            except httpx.HTTPError as exc:
                print(f"Runner result delivery error: {exc}", file=sys.stderr, flush=True)
                time.sleep(2.0)

    def _heartbeat_loop(self) -> None:
        while not self._stopping:
            try:
                response = self.client.post("/api/runner/heartbeat")
                response.raise_for_status()
            except httpx.HTTPError as exc:
                if not self._stopping:
                    print(f"Runner heartbeat error: {exc}", file=sys.stderr, flush=True)
            for _ in range(15):
                if self._stopping:
                    return
                time.sleep(1.0)


class _RunnerEventPublisher:
    """Send runner events off the model thread and coalesce adjacent token chunks."""

    _STOP = object()

    def __init__(self, client, job_id: str, logger):
        self.client = client
        self.job_id = job_id
        self.logger = logger
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(
            target=self._run,
            name=f"autocode-event-{job_id[:8]}",
            daemon=True,
        )
        self._thread.start()

    def emit(self, event: dict[str, Any]) -> None:
        self._queue.put(dict(event))

    def close(self) -> None:
        self._queue.put(self._STOP)
        self._thread.join(timeout=10.0)
        if self._thread.is_alive():
            log_event(
                self.logger,
                logging.ERROR,
                "Runner event publisher did not drain before timeout",
                job_id=self.job_id,
            )

    def _run(self) -> None:
        pending = None
        while True:
            item = pending if pending is not None else self._queue.get()
            pending = None
            if item is self._STOP:
                return
            event = dict(item)
            if event.get("type") == "token":
                text_parts = [str(event.get("text", ""))]
                while sum(len(part) for part in text_parts) < 512:
                    try:
                        next_item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if next_item is self._STOP:
                        pending = self._STOP
                        break
                    if isinstance(next_item, dict) and next_item.get("type") == "token":
                        text_parts.append(str(next_item.get("text", "")))
                        event["elapsed_ms"] = next_item.get("elapsed_ms", event.get("elapsed_ms"))
                    else:
                        pending = next_item
                        break
                event["text"] = "".join(text_parts)
            try:
                response = self.client.post(
                    f"/api/runner/event/{self.job_id}",
                    json=event,
                )
                if response.status_code == 404:
                    return
                response.raise_for_status()
            except Exception as exc:
                log_event(
                    self.logger,
                    logging.ERROR,
                    "Runner event delivery failed",
                    job_id=self.job_id,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )


def main() -> None:
    settings = load_runner_settings()
    migration_started = time.perf_counter()
    migrated_sessions = migrate_session_storage()
    if migrated_sessions:
        migration_ms = (time.perf_counter() - migration_started) * 1000
        print(
            f"Migrated {migrated_sessions} sessions into project storage "
            f"in {migration_ms:.1f}ms",
            flush=True,
        )
    runner = LocalRunner(settings)

    def stop_runner(*_: object) -> None:
        runner.stop()

    signal.signal(signal.SIGINT, stop_runner)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop_runner)
    try:
        print(
            f"AutoCode local runner connected to {settings.relay_url}; "
            f"workspace_registry={runner.registry.path}",
            flush=True,
        )
        runner.run_forever()
    finally:
        runner.close()


if __name__ == "__main__":
    main()
