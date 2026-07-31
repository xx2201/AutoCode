"""Stable prompt snapshots and model-step transaction identifiers."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class PromptSnapshot:
    """The cache-sensitive inputs frozen for one user turn."""

    turn_id: str
    system_prompt: str
    tool_schemas: tuple[dict, ...]
    tool_names: tuple[str, ...]
    digest: str

    @classmethod
    def create(
        cls,
        *,
        turn_id: str,
        system_prompt: str,
        tool_schemas: list[dict],
        tool_names: list[str],
    ) -> "PromptSnapshot":
        schemas = tuple(json.loads(json.dumps(item)) for item in tool_schemas)
        payload = json.dumps(
            {
                "turn_id": turn_id,
                "system_prompt": system_prompt,
                "tool_schemas": schemas,
                "tool_names": tool_names,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return cls(
            turn_id=turn_id,
            system_prompt=system_prompt,
            tool_schemas=schemas,
            tool_names=tuple(tool_names),
            digest=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        )

    def to_dict(self) -> dict:
        return {
            "turn_id": self.turn_id,
            "system_prompt": self.system_prompt,
            "tool_schemas": [dict(item) for item in self.tool_schemas],
            "tool_names": list(self.tool_names),
            "digest": self.digest,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PromptSnapshot":
        snapshot = cls.create(
            turn_id=str(data.get("turn_id") or ""),
            system_prompt=str(data.get("system_prompt") or ""),
            tool_schemas=list(data.get("tool_schemas") or []),
            tool_names=[str(item) for item in data.get("tool_names") or []],
        )
        stored_digest = str(data.get("digest") or "")
        if stored_digest and stored_digest != snapshot.digest:
            raise ValueError("Persisted PromptSnapshot digest does not match its contents.")
        return snapshot


def new_model_step_id() -> str:
    return f"step_{uuid.uuid4().hex}"
