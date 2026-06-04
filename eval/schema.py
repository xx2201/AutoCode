"""Task schema for the evaluation harness."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TextExpectation:
    path: str
    text: str


@dataclass
class OutcomeExpectations:
    must_contain: list[TextExpectation] = field(default_factory=list)
    must_contain_any: list[TextExpectation] = field(default_factory=list)
    must_not_contain: list[TextExpectation] = field(default_factory=list)
    must_change_files: list[str] = field(default_factory=list)
    must_change_any_files: list[str] = field(default_factory=list)
    must_not_change_files: list[str] = field(default_factory=list)
    verification_command: str = ""
    verification_must_contain: str = ""
    response_must_contain: list[str] = field(default_factory=list)
    response_must_not_contain: list[str] = field(default_factory=list)


@dataclass
class TrajectoryExpectations:
    must_use_tools: list[str] = field(default_factory=list)
    forbid_tools: list[str] = field(default_factory=list)
    require_todo: bool = False
    require_read_before_edit: list[str] = field(default_factory=list)


@dataclass
class SafetyExpectations:
    forbidden_modified_paths: list[str] = field(default_factory=list)
    approval_required_tools: list[str] = field(default_factory=list)
    no_workspace_escape: bool = True


@dataclass
class EfficiencyExpectations:
    max_steps: int = 0
    max_tool_calls: int = 0
    max_prompt_tokens: int = 0
    max_completion_tokens: int = 0
    max_duration_seconds: float = 0.0


@dataclass
class RecoveryExpectations:
    require_recovery_hint: bool = False
    require_retry_after_error: bool = False


@dataclass
class JudgeExpectations:
    enabled: bool = True
    min_score: float = 0.7
    focus: list[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class EvalTaskSpec:
    id: str
    title: str
    prompt: str
    fixture: str
    tags: list[str] = field(default_factory=list)
    trials: int = 1
    max_rounds: int = 20
    approval_mode: str = "approve_all"
    auto_approve: bool = False
    outcome: OutcomeExpectations = field(default_factory=OutcomeExpectations)
    trajectory: TrajectoryExpectations = field(default_factory=TrajectoryExpectations)
    safety: SafetyExpectations = field(default_factory=SafetyExpectations)
    efficiency: EfficiencyExpectations = field(default_factory=EfficiencyExpectations)
    recovery: RecoveryExpectations = field(default_factory=RecoveryExpectations)
    judge: JudgeExpectations = field(default_factory=JudgeExpectations)

    @classmethod
    def load(cls, path: str | Path) -> "EvalTaskSpec":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> "EvalTaskSpec":
        if not data.get("id"):
            raise ValueError("task id is required")
        if not data.get("prompt"):
            raise ValueError(f"task {data.get('id', '?')} is missing a prompt")
        if not data.get("fixture"):
            raise ValueError(f"task {data.get('id', '?')} is missing a fixture")

        def parse_text_items(items):
            return [TextExpectation(path=item["path"], text=item["text"]) for item in items]

        outcome = data.get("expectations", {}).get("outcome", {})
        trajectory = data.get("expectations", {}).get("trajectory", {})
        safety = data.get("expectations", {}).get("safety", {})
        efficiency = data.get("expectations", {}).get("efficiency", {})
        recovery = data.get("expectations", {}).get("recovery", {})
        judge = data.get("expectations", {}).get("judge", {})

        return cls(
            id=data["id"],
            title=data.get("title", data["id"]),
            prompt=data["prompt"],
            fixture=data["fixture"],
            tags=list(data.get("tags", [])),
            trials=int(data.get("trials", 1)),
            max_rounds=int(data.get("max_rounds", 20)),
            approval_mode=data.get("approval_mode", "approve_all"),
            auto_approve=bool(data.get("auto_approve", False)),
            outcome=OutcomeExpectations(
                must_contain=parse_text_items(outcome.get("must_contain", [])),
                must_contain_any=parse_text_items(outcome.get("must_contain_any", [])),
                must_not_contain=parse_text_items(outcome.get("must_not_contain", [])),
                must_change_files=list(outcome.get("must_change_files", [])),
                must_change_any_files=list(outcome.get("must_change_any_files", [])),
                must_not_change_files=list(outcome.get("must_not_change_files", [])),
                verification_command=outcome.get("verification_command", ""),
                verification_must_contain=outcome.get("verification_must_contain", ""),
                response_must_contain=list(outcome.get("response_must_contain", [])),
                response_must_not_contain=list(outcome.get("response_must_not_contain", [])),
            ),
            trajectory=TrajectoryExpectations(
                must_use_tools=list(trajectory.get("must_use_tools", [])),
                forbid_tools=list(trajectory.get("forbid_tools", [])),
                require_todo=bool(trajectory.get("require_todo", False)),
                require_read_before_edit=list(trajectory.get("require_read_before_edit", [])),
            ),
            safety=SafetyExpectations(
                forbidden_modified_paths=list(safety.get("forbidden_modified_paths", [])),
                approval_required_tools=list(safety.get("approval_required_tools", [])),
                no_workspace_escape=bool(safety.get("no_workspace_escape", True)),
            ),
            efficiency=EfficiencyExpectations(
                max_steps=int(efficiency.get("max_steps", 0)),
                max_tool_calls=int(efficiency.get("max_tool_calls", 0)),
                max_prompt_tokens=int(efficiency.get("max_prompt_tokens", 0)),
                max_completion_tokens=int(efficiency.get("max_completion_tokens", 0)),
                max_duration_seconds=float(efficiency.get("max_duration_seconds", 0.0)),
            ),
            recovery=RecoveryExpectations(
                require_recovery_hint=bool(recovery.get("require_recovery_hint", False)),
                require_retry_after_error=bool(recovery.get("require_retry_after_error", False)),
            ),
            judge=JudgeExpectations(
                enabled=bool(judge.get("enabled", True)),
                min_score=float(judge.get("min_score", 0.7)),
                focus=list(judge.get("focus", [])),
                notes=judge.get("notes", ""),
            ),
        )
