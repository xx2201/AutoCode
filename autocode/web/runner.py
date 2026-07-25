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
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values

from .. import __version__
from ..config import Config
from ..diagnostics import diagnostic_log_dir, get_diagnostic_logger, log_event
from ..mcp import get_shared_mcp_manager
from ..remote.manager import RemoteManager, presentation_tool_arguments
from ..tools.factory import build_agent_tools
from ..workspaces import WorkspaceRegistry
from .files import WebFileStore, WebSendTool
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

    def execute(self, action: str, payload: dict[str, Any], event_handler=None) -> Any:
        if action == "bootstrap":
            return {
                "model": self._base_config.model,
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
                if event in {"before_tool", "after_tool"}:
                    work_event = {
                        "type": "work",
                        "phase": "started" if event == "before_tool" else "completed",
                        "tool_call_id": str(data.get("tool_call_id", "")),
                        "tool_name": str(data.get("tool_name", "")),
                    }
                    if event == "before_tool":
                        work_event["arguments"] = presentation_tool_arguments(
                            data.get("arguments")
                        )
                    else:
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
                        )
                        if key in data
                    }
                    event_handler({"type": "stage", "stage": stage, "details": details})

            submit_kwargs = {}
            if event_handler is not None:
                submit_kwargs.update(hook_handler=on_hook, on_token=on_token)
            if payload.get("attachments"):
                submit_kwargs["attachments"] = list(payload["attachments"])
            with self._capture_output_files(str(payload["workspace_id"])) as output_files:
                result = manager.submit(
                    payload["client_id"],
                    payload.get("prompt", ""),
                    **submit_kwargs,
                )
            changed_files = changed_git_files(
                git_before,
                git_turn_snapshot(workspace),
            )
            manager.annotate_turn_changes(payload["client_id"], changed_files)
            if event_handler is not None:
                if first_token_seen:
                    event_handler({"type": "stage", "stage": "last_token"})
                event_handler({"type": "stage", "stage": "persisted"})
            response = asdict(result)
            response["files"] = output_files
            response["changed_files"] = changed_files
            return response
        if action == "approval":
            workspace = self.registry.resolve(workspace_id)
            git_before = git_turn_snapshot(workspace)
            with self._capture_output_files(str(payload["workspace_id"])) as output_files:
                result = manager.resolve_approval(
                    payload["client_id"],
                    payload["approved"],
                    payload["approve_all"],
                )
            changed_files = changed_git_files(
                git_before,
                git_turn_snapshot(workspace),
            )
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
            result = manager.resume_session(payload["client_id"], payload["session_id"])
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
                    self._run_job(job)
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

    def stop(self) -> None:
        self._stopping = True

    def close(self) -> None:
        if self._owns_client:
            self.client.close()
        for manager in self._managers.values():
            manager.close()
        self._managers.clear()

    def _manager(self, workspace_id: str):
        manager = self._managers.get(workspace_id)
        if manager is not None:
            return manager
        workspace = self.registry.resolve(workspace_id)
        manager = self._manager_factory(workspace)
        self._managers[workspace_id] = manager
        return manager

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
                    action=str(job.get("action", "")),
                    stage=str(enriched.get("stage", "")),
                    elapsed_ms=enriched["elapsed_ms"],
                    details=enriched.get("details") or {},
                )

        try:
            emit({"type": "stage", "stage": "runner_started"})
            result = self.execute(
                str(job["action"]),
                dict(job.get("payload") or {}),
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
            action=str(job.get("action", "")),
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
