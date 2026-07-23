"""Thread-safe relay broker between the browser API and a local runner."""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterator


class RunnerOfflineError(RuntimeError):
    """Raised when no local runner has checked in recently."""


class RelayTimeoutError(RuntimeError):
    """Raised when a dispatched job does not finish before its deadline."""


class RemoteExecutionError(RuntimeError):
    """Raised when the local runner reports an action failure."""

    def __init__(self, message: str, status_code: int = 500):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class _RelayJob:
    job_id: str
    action: str
    payload: dict[str, Any]
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: str = ""
    status_code: int = 500
    claimed: bool = False
    stream: bool = False
    completed: bool = False
    created_at: float = field(default_factory=time.monotonic)
    claimed_at: float | None = None
    completed_at: float | None = None
    events: deque[dict[str, Any]] = field(default_factory=deque)


class RelayBroker:
    """Coordinate one or more outbound-polling runners without public host ports."""

    def __init__(self, *, runner_ttl: float = 45.0):
        self._runner_ttl = runner_ttl
        self._condition = threading.Condition()
        self._queue: deque[_RelayJob] = deque()
        self._pending: dict[str, _RelayJob] = {}
        self._last_runner_seen = 0.0

    @property
    def runner_connected(self) -> bool:
        with self._condition:
            return self._runner_is_connected(time.monotonic())

    def touch_runner(self) -> None:
        with self._condition:
            self._last_runner_seen = time.monotonic()
            self._condition.notify_all()

    def dispatch(
        self,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = 3600.0,
    ) -> Any:
        job = self._enqueue(action, payload or {}, stream=False)

        if not job.event.wait(timeout):
            with self._condition:
                self._pending.pop(job.job_id, None)
                if not job.claimed:
                    try:
                        self._queue.remove(job)
                    except ValueError:
                        pass
            raise RelayTimeoutError("本机任务执行超时。")

        if job.error:
            raise RemoteExecutionError(job.error, job.status_code)
        return job.result

    def start_stream(self, action: str, payload: dict[str, Any] | None = None) -> str:
        """Enqueue a streaming job and return its opaque id."""
        return self._enqueue(action, payload or {}, stream=True).job_id

    def iter_stream(self, job_id: str, *, timeout: float = 3600.0) -> Iterator[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        try:
            while True:
                with self._condition:
                    job = self._pending.get(job_id)
                    if job is None:
                        yield {
                            "type": "error",
                            "error": "Relay stream no longer exists.",
                            "status_code": 404,
                        }
                        return
                    while not job.events and not job.completed:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            job.completed = True
                            job.error = "本机任务执行超时。"
                            job.status_code = 504
                            job.completed_at = time.monotonic()
                            break
                        self._condition.wait(remaining)
                    if job.events:
                        event = job.events.popleft()
                    elif job.completed:
                        event = self._completion_event(job)
                    else:
                        continue

                yield event
                if event["type"] in {"result", "error"}:
                    return
        finally:
            with self._condition:
                self._pending.pop(job_id, None)
                self._condition.notify_all()

    def publish(self, job_id: str, event: dict[str, Any]) -> bool:
        """Publish a runner event to a waiting browser stream."""
        with self._condition:
            job = self._pending.get(job_id)
            if job is None or not job.stream or job.completed:
                return False
            job.events.append(dict(event))
            self._condition.notify_all()
            return True

    def next_job(self, *, wait: float = 25.0) -> dict[str, Any] | None:
        deadline = time.monotonic() + max(0.0, wait)
        with self._condition:
            self.touch_runner()
            while not self._queue:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.touch_runner()
                    return None
                self._condition.wait(remaining)
                self.touch_runner()

            job = self._queue.popleft()
            job.claimed = True
            job.claimed_at = time.monotonic()
            if job.stream:
                job.events.append(
                    {
                        "type": "stage",
                        "stage": "claimed",
                        "elapsed_ms": round((job.claimed_at - job.created_at) * 1000, 1),
                    }
                )
                self._condition.notify_all()
            return {
                "job_id": job.job_id,
                "action": job.action,
                "payload": job.payload,
                "stream": job.stream,
            }

    def complete(
        self,
        job_id: str,
        *,
        success: bool,
        result: Any = None,
        error: str = "",
        status_code: int = 500,
    ) -> bool:
        with self._condition:
            self._last_runner_seen = time.monotonic()
            job = self._pending.get(job_id)
            if job is None:
                return False
            if success:
                job.result = result
            else:
                job.error = error or "本机 Runner 执行失败。"
                job.status_code = status_code
            job.completed = True
            job.completed_at = time.monotonic()
            if not job.stream:
                self._pending.pop(job_id, None)
            job.event.set()
            self._condition.notify_all()
            return True

    def _runner_is_connected(self, now: float) -> bool:
        return self._last_runner_seen > 0 and now - self._last_runner_seen <= self._runner_ttl

    def _enqueue(self, action: str, payload: dict[str, Any], *, stream: bool) -> _RelayJob:
        job = _RelayJob(
            job_id=uuid.uuid4().hex,
            action=action,
            payload=payload,
            stream=stream,
        )
        with self._condition:
            if not self._runner_is_connected(time.monotonic()):
                raise RunnerOfflineError("本机 Runner 未连接，请确认电脑已开机且 Runner 正在运行。")
            self._pending[job.job_id] = job
            self._queue.append(job)
            self._condition.notify_all()
        return job

    @staticmethod
    def _completion_event(job: _RelayJob) -> dict[str, Any]:
        finished_at = job.completed_at or time.monotonic()
        timings = {
            "dispatch_to_claim_ms": (
                round((job.claimed_at - job.created_at) * 1000, 1)
                if job.claimed_at is not None
                else None
            ),
            "relay_total_ms": round((finished_at - job.created_at) * 1000, 1),
        }
        if job.error:
            return {
                "type": "error",
                "error": job.error,
                "status_code": job.status_code,
                "timings": timings,
            }
        return {"type": "result", "data": job.result, "timings": timings}
