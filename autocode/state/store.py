"""Session record persistence."""

from __future__ import annotations

import json
import time

from .checkpoint import list_sessions, session_dir
from .model import SessionState


class SessionStore:
    def sync(self, session_state: SessionState, model: str):
        directory = session_dir(session_state.session_id)
        directory.mkdir(parents=True, exist_ok=True)
        current_turn = session_state.current_turn
        session_payload = {
            "session_id": session_state.session_id,
            "title": session_state.title or (current_turn.title if current_turn else ""),
            "turn_id": current_turn.turn_id if current_turn else "",
            "turn_title": current_turn.title if current_turn else "",
            "status": current_turn.status if current_turn else "idle",
            "step_index": current_turn.step_index if current_turn else 0,
            "transcript_file": "transcript.jsonl",
            "current_turn_file": "current_turn.json",
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "model": model,
        }
        (directory / "session.json").write_text(json.dumps(session_payload, ensure_ascii=False, indent=2), encoding="utf-8")

        current_turn_payload = current_turn.to_dict() if current_turn else None
        (directory / "current_turn.json").write_text(
            json.dumps(current_turn_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def load(session_id: str) -> dict | None:
        path = session_dir(session_id) / "session.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def recent_session_summaries(limit: int = 3) -> list[str]:
        items = []
        for entry in list_sessions()[:limit]:
            items.append(
                f"- {entry['session_id']} ({entry['status']}, step {entry['step_index']}, model {entry['model']})"
            )
        return items
