"""Optional LLM judge for qualitative agent evaluation."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from corecoder.config import _load_dotenv
from corecoder.llm import LLM

from .graders import GradeResult, TrialArtifacts
from .schema import EvalTaskSpec


@dataclass
class JudgeConfig:
    model: str
    api_key: str
    base_url: str | None = None
    provider: str = "openai"
    temperature: float = 0.0
    max_tokens: int = 800

    @classmethod
    def from_env(cls, model_override: str | None = None) -> "JudgeConfig | None":
        _load_dotenv()
        api_key = os.getenv("CORECODER_EVAL_API_KEY") or os.getenv("DASHSCOPE_API_KEY") or ""
        model = model_override or os.getenv("CORECODER_EVAL_MODEL", "")
        if not api_key or not model:
            return None
        return cls(
            model=model,
            api_key=api_key,
            base_url=(
                os.getenv("CORECODER_EVAL_BASE_URL")
                or os.getenv("DASHSCOPE_BASE_URL")
                or "https://dashscope.aliyuncs.com/compatible-mode/v1"
            ),
            provider=os.getenv("CORECODER_EVAL_PROVIDER", "openai"),
            temperature=float(os.getenv("CORECODER_EVAL_TEMPERATURE", "0")),
            max_tokens=int(os.getenv("CORECODER_EVAL_MAX_TOKENS", "800")),
        )


@dataclass
class JudgeVerdict:
    passed: bool
    score: float
    summary: str
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)


class LLMJudge:
    def __init__(self, config: JudgeConfig):
        if config.provider != "openai":
            raise ValueError("only openai-compatible judge providers are supported")
        self.config = config
        self.llm = LLM(
            model=config.model,
            api_key=config.api_key,
            base_url=config.base_url,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )

    def evaluate(self, spec: EvalTaskSpec, artifacts: TrialArtifacts) -> GradeResult:
        verdict = self._judge(spec, artifacts)
        return GradeResult(
            name="llm_judge",
            passed=verdict.passed,
            score=verdict.score,
            summary=verdict.summary,
            details={
                "model": self.config.model,
                "strengths": verdict.strengths,
                "weaknesses": verdict.weaknesses,
            },
        )

    def _judge(self, spec: EvalTaskSpec, artifacts: TrialArtifacts) -> JudgeVerdict:
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a strict coding-agent evaluator. "
                    "Review the task, final response, trace, audit events, and verification result. "
                    "Return JSON only with keys: passed, score, summary, strengths, weaknesses. "
                    "Score must be between 0 and 1."
                ),
            },
            {
                "role": "user",
                "content": self._build_prompt(spec, artifacts),
            },
        ]
        response = self.llm.chat(messages)
        return self._parse_verdict(response.content, spec.judge.min_score)

    def _build_prompt(self, spec: EvalTaskSpec, artifacts: TrialArtifacts) -> str:
        changed_files = sorted(_changed_files(artifacts))
        tool_sequence = [
            entry["payload"].get("tool_name", "")
            for entry in artifacts.audit
            if entry["event"] == "before_tool"
        ]
        payload = {
            "task": {
                "id": spec.id,
                "title": spec.title,
                "prompt": spec.prompt,
                "judge_focus": spec.judge.focus,
                "judge_notes": spec.judge.notes,
                "min_score": spec.judge.min_score,
            },
            "final_response": artifacts.final_response,
            "trace": artifacts.trace,
            "changed_files": changed_files,
            "tool_sequence": tool_sequence,
            "recent_audit_events": artifacts.audit[-12:],
            "verification": None if artifacts.verification is None else {
                "exit_code": artifacts.verification.exit_code,
                "output": artifacts.verification.output,
            },
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _parse_verdict(self, content: str, min_score: float) -> JudgeVerdict:
        payload = self._extract_json(content)
        score = max(0.0, min(float(payload.get("score", 0.0)), 1.0))
        passed = bool(payload.get("passed", score >= min_score)) and score >= min_score
        return JudgeVerdict(
            passed=passed,
            score=score,
            summary=str(payload.get("summary", "")).strip() or "no summary",
            strengths=[str(item) for item in payload.get("strengths", [])][:5],
            weaknesses=[str(item) for item in payload.get("weaknesses", [])][:5],
        )

    def _extract_json(self, content: str) -> dict:
        text = content.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise ValueError(f"judge did not return valid JSON: {content}")


def _changed_files(artifacts: TrialArtifacts) -> set[str]:
    changed = artifacts.trace.get("modified_files", []) if artifacts.trace else []
    normalized: set[str] = set()
    for path in changed:
        value = path.replace("\\", "/").lstrip("./")
        candidate = Path(path)
        if candidate.is_absolute():
            try:
                value = candidate.resolve().relative_to(artifacts.workspace.resolve()).as_posix()
            except (OSError, ValueError):
                value = candidate.as_posix()
        normalized.add(value)
    return normalized
