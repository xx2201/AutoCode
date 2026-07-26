"""Thread-safe control messages for an active agent turn."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field


def new_message_id() -> str:
    return f"message_{uuid.uuid4().hex}"


def new_revision_id() -> str:
    return f"revision_{uuid.uuid4().hex}"


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True)
class TurnInput:
    """One user input addressed to the active turn or the next-turn queue."""

    content: str
    message_id: str = field(default_factory=new_message_id)
    expected_turn_id: str = ""
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return {
            "message_id": self.message_id,
            "content": self.content,
            "expected_turn_id": self.expected_turn_id,
            "created_at": self.created_at,
        }


class TurnController:
    """Coordinate steer input and queued follow-ups without taking the agent lock."""

    def __init__(self):
        self._lock = threading.RLock()
        self._active_turn_id = ""
        self._active = False
        self._steer_inbox: list[TurnInput] = []
        self._next_turn_queue: list[TurnInput] = []
        self._message_ids: set[str] = set()

    @property
    def active_turn_id(self) -> str:
        with self._lock:
            return self._active_turn_id if self._active else ""

    def start_turn(self, turn_id: str) -> None:
        if not turn_id:
            raise ValueError("turn_id is required")
        with self._lock:
            if self._active and self._active_turn_id != turn_id:
                raise ValueError(f"Turn '{self._active_turn_id}' is still active.")
            self._active_turn_id = turn_id
            self._active = True

    def finish_turn(self, turn_id: str) -> None:
        with self._lock:
            if not self._active and self._active_turn_id == turn_id:
                return
            self._validate_active(turn_id)
            self._active = False

    def steer(
        self,
        content: str,
        *,
        expected_turn_id: str,
        message_id: str | None = None,
    ) -> TurnInput:
        normalized = content.strip()
        if not normalized:
            raise ValueError("Steer content is required.")
        with self._lock:
            self._validate_active(expected_turn_id)
            item = self._new_input(normalized, expected_turn_id, message_id)
            self._steer_inbox.append(item)
            return item

    def drain_steer(self, expected_turn_id: str) -> list[TurnInput]:
        with self._lock:
            self._validate_active(expected_turn_id)
            items = self._steer_inbox
            self._steer_inbox = []
            return items

    def drain_steer_or_finish(self, expected_turn_id: str) -> tuple[list[TurnInput], bool]:
        """Atomically consume pending steer input or close an otherwise idle turn."""
        with self._lock:
            self._validate_active(expected_turn_id)
            if self._steer_inbox:
                items = self._steer_inbox
                self._steer_inbox = []
                return items, False
            self._active = False
            return [], True

    def queue(
        self,
        content: str,
        *,
        expected_turn_id: str = "",
        message_id: str | None = None,
    ) -> TurnInput:
        normalized = content.strip()
        if not normalized:
            raise ValueError("Queued content is required.")
        with self._lock:
            if expected_turn_id:
                self._validate_active(expected_turn_id)
            item = self._new_input(normalized, expected_turn_id, message_id)
            self._next_turn_queue.append(item)
            return item

    def queued(self) -> list[TurnInput]:
        with self._lock:
            return list(self._next_turn_queue)

    def restore_queued(self, items: list[dict]) -> None:
        """Restore persisted FIFO items from a backward-compatible checkpoint."""
        restored = []
        with self._lock:
            for data in items:
                if not isinstance(data, dict) or not str(data.get("content", "")).strip():
                    continue
                message_id = str(data.get("message_id") or new_message_id())
                if message_id in self._message_ids:
                    continue
                item = TurnInput(
                    message_id=message_id,
                    content=str(data["content"]),
                    expected_turn_id=str(data.get("expected_turn_id", "")),
                    created_at=str(data.get("created_at") or _now()),
                )
                self._message_ids.add(message_id)
                restored.append(item)
            self._next_turn_queue = restored

    def update_queued(self, message_id: str, content: str) -> TurnInput:
        normalized = content.strip()
        if not normalized:
            raise ValueError("Queued content is required.")
        with self._lock:
            for index, item in enumerate(self._next_turn_queue):
                if item.message_id == message_id:
                    updated = TurnInput(
                        message_id=item.message_id,
                        content=normalized,
                        expected_turn_id=item.expected_turn_id,
                        created_at=item.created_at,
                    )
                    self._next_turn_queue[index] = updated
                    return updated
        raise ValueError(f"Queued message '{message_id}' not found.")

    def delete_queued(self, message_id: str) -> None:
        with self._lock:
            for index, item in enumerate(self._next_turn_queue):
                if item.message_id == message_id:
                    self._next_turn_queue.pop(index)
                    self._message_ids.discard(message_id)
                    return
        raise ValueError(f"Queued message '{message_id}' not found.")

    def pop_queued(self) -> TurnInput | None:
        with self._lock:
            if not self._next_turn_queue:
                return None
            item = self._next_turn_queue.pop(0)
            self._message_ids.discard(item.message_id)
            return item

    def _new_input(self, content: str, expected_turn_id: str, message_id: str | None) -> TurnInput:
        resolved_id = message_id or new_message_id()
        if resolved_id in self._message_ids:
            raise ValueError(f"Message '{resolved_id}' already exists.")
        self._message_ids.add(resolved_id)
        return TurnInput(
            message_id=resolved_id,
            content=content,
            expected_turn_id=expected_turn_id,
        )

    def _validate_active(self, expected_turn_id: str) -> None:
        if not expected_turn_id:
            raise ValueError("expected_turn_id is required")
        if not self._active:
            raise ValueError("There is no active turn.")
        if self._active_turn_id != expected_turn_id:
            raise ValueError(
                f"Expected turn '{expected_turn_id}', but active turn is '{self._active_turn_id}'."
            )
