"""Local AutoCode runner that connects outward to the Web relay."""

from __future__ import annotations

import os
import signal
import ssl
import sys
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values

from .. import __version__
from ..config import Config
from ..remote.manager import RemoteManager


@dataclass(frozen=True)
class RunnerSettings:
    relay_url: str
    token: str
    ca_cert: str
    workspace_root: str
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
    workspace_root = _setting(values, "AUTOCODE_WORKSPACE_ROOT", str(Path.cwd()))
    poll_wait = float(_setting(values, "AUTOCODE_RUNNER_POLL_WAIT", "25"))

    if not relay_url.startswith("https://"):
        raise RuntimeError("AUTOCODE_RELAY_URL must use HTTPS.")
    if len(token) < 24:
        raise RuntimeError("AUTOCODE_RUNNER_TOKEN must contain at least 24 characters.")
    if not ca_cert or not Path(ca_cert).expanduser().is_file():
        raise RuntimeError("AUTOCODE_RELAY_CA_CERT must point to the relay CA certificate.")
    workspace = Path(workspace_root).expanduser().resolve()
    if not workspace.is_dir():
        raise RuntimeError(f"Workspace does not exist: {workspace}")
    return RunnerSettings(
        relay_url=relay_url,
        token=token,
        ca_cert=str(Path(ca_cert).expanduser().resolve()),
        workspace_root=str(workspace),
        poll_wait=max(1.0, min(poll_wait, 30.0)),
    )


class LocalRunner:
    """Execute relayed actions against one local RemoteManager."""

    def __init__(
        self,
        settings: RunnerSettings,
        *,
        manager: RemoteManager | Any | None = None,
        client: httpx.Client | Any | None = None,
    ):
        self.settings = settings
        self._owns_manager = manager is None
        if manager is None:
            agent_config = replace(
                Config.from_env(),
                workspace_root=settings.workspace_root,
            )
            if not agent_config.model:
                raise RuntimeError("AUTOCODE_MODEL is required in the local workspace .env.")
            if not agent_config.api_key:
                raise RuntimeError("AUTOCODE_API_KEY is required in the local workspace .env.")
            manager = RemoteManager(agent_config)
        self.manager = manager
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

    def execute(self, action: str, payload: dict[str, Any]) -> Any:
        manager = self.manager
        if action == "bootstrap":
            return {
                "model": manager.config.model,
                "workspace": Path(manager.config.workspace_root).name,
                "workspace_path": str(Path(manager.config.workspace_root).resolve()),
                "sessions": manager.list_resume_candidates(20),
                "version": __version__,
            }
        if action == "chat":
            return asdict(manager.submit(payload["client_id"], payload["prompt"]))
        if action == "approval":
            return asdict(
                manager.resolve_approval(
                    payload["client_id"],
                    payload["approved"],
                    payload["approve_all"],
                )
            )
        if action == "sessions":
            return {"sessions": manager.list_resume_candidates(payload.get("limit", 50))}
        if action == "resume":
            result = manager.resume_session(payload["client_id"], payload["session_id"])
            return {
                "result": asdict(result),
                "messages": manager.conversation_messages(payload["client_id"]),
            }
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
        if self._owns_manager:
            self.manager.close()

    def _run_job(self, job: dict[str, Any]) -> None:
        job_id = str(job["job_id"])
        try:
            result = self.execute(str(job["action"]), dict(job.get("payload") or {}))
            body = {"success": True, "result": result}
        except ValueError as exc:
            body = {"success": False, "error": str(exc), "status_code": 409}
        except Exception as exc:
            body = {
                "success": False,
                "error": f"{type(exc).__name__}: {exc}",
                "status_code": 500,
            }

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
            f"workspace={settings.workspace_root}",
            flush=True,
        )
        runner.run_forever()
    finally:
        runner.close()


if __name__ == "__main__":
    main()
