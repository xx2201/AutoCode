"""Optional LLM judge for qualitative agent evaluation."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from autocode.config import _load_dotenv
from autocode.llm import LLM

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
        api_key = os.getenv("AUTOCODE_EVAL_API_KEY") or os.getenv("DASHSCOPE_API_KEY") or ""
        model = model_override or os.getenv("AUTOCODE_EVAL_MODEL", "")
        if not api_key or not model:
            return None
        return cls(
            model=model,
            api_key=api_key,
            base_url=(
                os.getenv("AUTOCODE_EVAL_BASE_URL")
                or os.getenv("DASHSCOPE_BASE_URL")
                or "https://dashscope.aliyuncs.com/compatible-mode/v1"
            ),
            provider=os.getenv("AUTOCODE_EVAL_PROVIDER", "openai"),
            temperature=float(os.getenv("AUTOCODE_EVAL_TEMPERATURE", "0")),
            max_tokens=int(os.getenv("AUTOCODE_EVAL_MAX_TOKENS", "800")),
        )


@dataclass
class JudgeVerdict:
    passed: bool
    score: float
    judge_total: float
    summary: str
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    dimensions: dict = field(default_factory=dict)


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
        return self.evaluate_consensus(spec, artifacts, repeats=1)

    def evaluate_consensus(
        self,
        spec: EvalTaskSpec,
        artifacts: TrialArtifacts,
        *,
        repeats: int = 2,
        arbitration_dimension_delta: float = 3.0,
    ) -> GradeResult:
        verdicts = [self._judge(spec, artifacts) for _ in range(max(1, repeats))]
        arbitration_triggered = (
            len(verdicts) >= 2
            and self._max_dimension_delta(verdicts[0], verdicts[1]) >= arbitration_dimension_delta
        )
        if arbitration_triggered:
            verdicts.append(self._judge(spec, artifacts))
        verdict = self._merge_verdicts(verdicts, spec.judge.min_score)
        result = GradeResult(
            name="llm_judge",
            passed=verdict.passed,
            score=verdict.score,
            summary=verdict.summary,
            details={
                "model": self.config.model,
                "judge_total": verdict.judge_total,
                "dimensions": verdict.dimensions,
                "strengths": verdict.strengths,
                "weaknesses": verdict.weaknesses,
                "judge_runs": len(verdicts),
                "arbitration_triggered": arbitration_triggered,
            },
        )
        return result

    def _judge(self, spec: EvalTaskSpec, artifacts: TrialArtifacts) -> JudgeVerdict:
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a strict blind evaluator for anonymous coding-agent outputs. "
                    "Do not infer platform identity. "
                    "Review the implementation using six dimensions: "
                    "D1 requirement completion, D2 correctness, D3 code quality, "
                    "D4 minimal change, D5 verification awareness, D6 implementation clarity. "
                    "Review the task, final response, changed files, execution summary, and verification result. "
                    "Return JSON only with keys: passed, score, judge_total, summary, strengths, weaknesses, dimensions. "
                    "judge_total must be 0-60. score must equal judge_total/60. "
                    "dimensions must contain D1..D6, each with score (0-10) and evidence."
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
        trace = artifacts.trace or {}
        execution_summary = {
            "turns": int(trace.get("llm_calls") or trace.get("steps") or 0),
            "tool_calls": int(trace.get("tool_calls") or 0),
            "duration_seconds": float(trace.get("duration_seconds") or 0.0),
            "prompt_tokens": int(trace.get("prompt_tokens") or 0),
            "completion_tokens": int(trace.get("completion_tokens") or 0),
        }
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
            "changed_files": changed_files,
            "execution_summary": execution_summary,
            "verification": None if artifacts.verification is None else {
                "exit_code": artifacts.verification.exit_code,
                "output": artifacts.verification.output,
                "commands": [
                    {
                        "command": item.command,
                        "exit_code": item.exit_code,
                    }
                    for item in artifacts.verification.commands
                ],
            },
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _parse_verdict(self, content: str, min_score: float) -> JudgeVerdict:
        payload = self._extract_json(content)
        dimensions = payload.get("dimensions", {})
        dimension_total = 0.0
        has_dimension_scores = False
        for key in ("D1", "D2", "D3", "D4", "D5", "D6"):
            item = dimensions.get(key)
            if not isinstance(item, dict) or "score" not in item:
                continue
            has_dimension_scores = True
            dimension_total += max(0.0, min(float(item.get("score", 0.0)), 10.0))
        if "judge_total" in payload:
            judge_total = max(0.0, min(float(payload.get("judge_total", 0.0)), 60.0))
        elif has_dimension_scores:
            judge_total = max(0.0, min(dimension_total, 60.0))
        else:
            judge_total = max(0.0, min(float(payload.get("score", 0.0)) * 60.0, 60.0))
        judge_total = round(judge_total, 1)
        score = round(judge_total / 60.0, 4)
        passed = bool(payload.get("passed", score >= min_score)) and score >= min_score
        return JudgeVerdict(
            passed=passed,
            score=score,
            judge_total=judge_total,
            summary=str(payload.get("summary") or payload.get("one_line_verdict") or "").strip() or "no summary",
            strengths=[str(item) for item in payload.get("strengths", [])][:5],
            weaknesses=[str(item) for item in payload.get("weaknesses", [])][:5],
            dimensions=dimensions,
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

    def _merge_verdicts(self, verdicts: list[JudgeVerdict], min_score: float) -> JudgeVerdict:
        dimensions: dict[str, dict] = {}
        for key in ("D1", "D2", "D3", "D4", "D5", "D6"):
            items = [item.dimensions.get(key) for item in verdicts if isinstance(item.dimensions.get(key), dict)]
            if not items:
                continue
            avg_score = round(
                sum(max(0.0, min(float(item.get("score", 0.0)), 10.0)) for item in items) / len(items),
                1,
            )
            evidence = next((str(item.get("evidence", "")).strip() for item in items if str(item.get("evidence", "")).strip()), "")
            dimensions[key] = {"score": avg_score, "evidence": evidence}
        judge_total = round(sum(item.judge_total for item in verdicts) / len(verdicts), 1)
        score = round(judge_total / 60.0, 4)
        strengths = _dedupe_list(text for item in verdicts for text in item.strengths)[:5]
        weaknesses = _dedupe_list(text for item in verdicts for text in item.weaknesses)[:5]
        if len(verdicts) == 1:
            summary = verdicts[0].summary
        else:
            summary = f"Consensus from {len(verdicts)} blind judge runs. Average total {judge_total}/60."
        return JudgeVerdict(
            passed=score >= min_score,
            score=score,
            judge_total=judge_total,
            summary=summary,
            strengths=strengths,
            weaknesses=weaknesses,
            dimensions=dimensions,
        )

    def _max_dimension_delta(self, left: JudgeVerdict, right: JudgeVerdict) -> float:
        deltas = []
        for key in ("D1", "D2", "D3", "D4", "D5", "D6"):
            left_score = _dimension_score(left.dimensions, key)
            right_score = _dimension_score(right.dimensions, key)
            if left_score is None or right_score is None:
                continue
            deltas.append(abs(left_score - right_score))
        return max(deltas, default=0.0)


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


def _dimension_score(dimensions: dict, key: str) -> float | None:
    item = dimensions.get(key)
    if not isinstance(item, dict) or "score" not in item:
        return None
    return max(0.0, min(float(item.get("score", 0.0)), 10.0))


def _dedupe_list(items) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        ordered.append(text)
    return ordered

