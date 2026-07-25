"""Report generation for evaluation runs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .graders import GradeResult, TrialArtifacts
from .schema import EvalTaskSpec


@dataclass
class TrialReport:
    agent_provider: str
    task_id: str
    title: str
    trial_index: int
    passed: bool
    score: float
    composite: float
    gate_score: float
    judge_score: float
    grade: str
    graders: list[GradeResult]
    turns: int = 0
    duration_seconds: float = 0.0
    tool_calls: int = 0
    input_tokens_total: int = 0
    output_tokens_total: int = 0
    effective_input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_miss_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_hit_rate: float = 0.0
    human_assist: bool = False
    timeout_abort: bool = False


def build_trial_report(
    spec: EvalTaskSpec,
    trial_index: int,
    grader_results: list[GradeResult],
    agent_provider: str = "autocode",
    artifacts: TrialArtifacts | None = None,
) -> TrialReport:
    gate = _find_grader(grader_results, "gate")
    judge = _find_grader(grader_results, "llm_judge")
    if gate or judge:
        gate_score = _gate_score(gate)
        judge_score = _judge_score(judge)
        composite = round(gate_score + judge_score, 1)
        score = composite / 100.0
        passed = gate.passed if gate else _legacy_passed(grader_results)
        grade = _grade_from_composite(composite, _gate_g1(gate))
    else:
        score = (sum(item.score for item in grader_results) / len(grader_results)) if grader_results else 0.0
        composite = round(score * 100.0, 1)
        gate_score = 0.0
        judge_score = 0.0
        passed = _legacy_passed(grader_results)
        grade = _base_grade(composite)

    trace = artifacts.trace if artifacts is not None else {}
    turns = int(trace.get("llm_calls") or trace.get("steps") or 0)
    duration_seconds = float(trace.get("duration_seconds") or 0.0)
    tool_calls = int(trace.get("tool_calls") or 0)
    input_tokens_total = int(trace.get("input_tokens_total") or trace.get("prompt_tokens") or 0)
    output_tokens_total = int(trace.get("output_tokens_total") or trace.get("completion_tokens") or 0)
    effective_input_tokens = int(trace.get("effective_input_tokens") or input_tokens_total or 0)
    cache_read_tokens = int(trace.get("cache_read_tokens") or 0)
    cache_miss_tokens = int(trace.get("cache_miss_tokens") or 0)
    cache_creation_tokens = int(trace.get("cache_creation_tokens") or 0)
    cache_hit_rate = float(trace.get("cache_hit_rate") or 0.0)
    human_assist = bool(trace.get("human_assist") or False)
    timeout_abort = str(trace.get("stop_reason") or "").lower() in {"timeout", "timeout_abort"}
    return TrialReport(
        agent_provider=agent_provider,
        task_id=spec.id,
        title=spec.title,
        trial_index=trial_index,
        passed=passed,
        score=score,
        composite=composite,
        gate_score=gate_score,
        judge_score=judge_score,
        grade=grade,
        graders=grader_results,
        turns=turns,
        duration_seconds=duration_seconds,
        tool_calls=tool_calls,
        input_tokens_total=input_tokens_total,
        output_tokens_total=output_tokens_total,
        effective_input_tokens=effective_input_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_miss_tokens=cache_miss_tokens,
        cache_creation_tokens=cache_creation_tokens,
        cache_hit_rate=cache_hit_rate,
        human_assist=human_assist,
        timeout_abort=timeout_abort,
    )


def aggregate_reports(reports: list[TrialReport]) -> dict:
    if not reports:
        return {
            "total_trials": 0,
            "pass_rate": 0.0,
            "objective_success_rate": 0.0,
            "average_score": 0.0,
            "average_composite": 0.0,
            "reports": [],
            "by_agent": {},
        }
    passed = sum(1 for item in reports if item.passed)
    avg_score = sum(item.score for item in reports) / len(reports)
    avg_composite = sum(item.composite for item in reports) / len(reports)
    by_agent: dict[str, dict] = {}
    for report in reports:
        bucket = by_agent.setdefault(
            report.agent_provider,
            {
                "total_trials": 0,
                "passed": 0,
                "score_sum": 0.0,
                "composite_sum": 0.0,
                "sa_count": 0,
                "turns_sum": 0,
                "turns_count": 0,
                "duration_sum": 0.0,
                "duration_count": 0,
                "input_tokens_sum": 0,
                "output_tokens_sum": 0,
                "effective_input_tokens_sum": 0,
                "cache_read_sum": 0,
                "cache_miss_sum": 0,
                "cache_creation_sum": 0,
                "fallback_count": 0,
            },
        )
        bucket["total_trials"] += 1
        bucket["passed"] += 1 if report.passed else 0
        bucket["score_sum"] += report.score
        bucket["composite_sum"] += report.composite
        bucket["sa_count"] += 1 if report.grade in {"S", "A"} else 0
        if report.turns > 0:
            bucket["turns_sum"] += report.turns
            bucket["turns_count"] += 1
        if report.duration_seconds > 0:
            bucket["duration_sum"] += report.duration_seconds
            bucket["duration_count"] += 1
        bucket["input_tokens_sum"] += report.input_tokens_total
        bucket["output_tokens_sum"] += report.output_tokens_total
        bucket["effective_input_tokens_sum"] += report.effective_input_tokens
        bucket["cache_read_sum"] += report.cache_read_tokens
        bucket["cache_miss_sum"] += report.cache_miss_tokens
        bucket["cache_creation_sum"] += report.cache_creation_tokens
        bucket["fallback_count"] += 1 if report.human_assist or report.timeout_abort else 0
    return {
        "total_trials": len(reports),
        "pass_rate": passed / len(reports),
        "objective_success_rate": passed / len(reports),
        "average_score": avg_score,
        "average_composite": avg_composite,
        "by_agent": {
            agent: {
                "total_trials": data["total_trials"],
                "pass_rate": data["passed"] / data["total_trials"],
                "objective_success_rate": data["passed"] / data["total_trials"],
                "average_score": data["score_sum"] / data["total_trials"],
                "average_composite": data["composite_sum"] / data["total_trials"],
                "s_or_a_rate": data["sa_count"] / data["total_trials"],
                "avg_turns": (
                    data["turns_sum"] / data["turns_count"]
                    if data["turns_count"]
                    else 0.0
                ),
                "avg_duration_seconds": (
                    data["duration_sum"] / data["duration_count"]
                    if data["duration_count"]
                    else 0.0
                ),
                "average_input_tokens_total": data["input_tokens_sum"] / data["total_trials"],
                "average_output_tokens_total": data["output_tokens_sum"] / data["total_trials"],
                "average_effective_input_tokens": data["effective_input_tokens_sum"] / data["total_trials"],
                "average_cache_read_tokens": data["cache_read_sum"] / data["total_trials"],
                "average_cache_miss_tokens": data["cache_miss_sum"] / data["total_trials"],
                "average_cache_creation_tokens": data["cache_creation_sum"] / data["total_trials"],
                "fallback_rate": data["fallback_count"] / data["total_trials"],
            }
            for agent, data in sorted(by_agent.items())
        },
        "reports": [serialize_trial_report(item) for item in reports],
    }


def serialize_trial_report(report: TrialReport) -> dict:
    return {
        "agent_provider": report.agent_provider,
        "task_id": report.task_id,
        "title": report.title,
        "trial_index": report.trial_index,
        "passed": report.passed,
        "score": report.score,
        "composite": report.composite,
        "gate_score": report.gate_score,
        "judge_score": report.judge_score,
        "grade": report.grade,
        "turns": report.turns,
        "duration_seconds": report.duration_seconds,
        "tool_calls": report.tool_calls,
        "input_tokens_total": report.input_tokens_total,
        "output_tokens_total": report.output_tokens_total,
        "effective_input_tokens": report.effective_input_tokens,
        "cache_read_tokens": report.cache_read_tokens,
        "cache_miss_tokens": report.cache_miss_tokens,
        "cache_creation_tokens": report.cache_creation_tokens,
        "cache_hit_rate": report.cache_hit_rate,
        "human_assist": report.human_assist,
        "timeout_abort": report.timeout_abort,
        "graders": [
            {
                "name": grader.name,
                "passed": grader.passed,
                "score": grader.score,
                "summary": grader.summary,
                "details": grader.details,
            }
            for grader in report.graders
        ],
    }


def write_report(output_dir: Path, summary: dict):
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "report.md").write_text(render_markdown(summary), encoding="utf-8")


def render_markdown(summary: dict) -> str:
    lines = [
        "# Eval Report",
        "",
        f"- Total trials: {summary['total_trials']}",
        f"- Objective success rate: {summary['objective_success_rate']:.2%}",
        f"- Average composite: {summary['average_composite']:.1f}/100",
    ]
    run_config = summary.get("run_config")
    if run_config:
        lines.append(f"- Agent model: {run_config.get('agent_model', '')}")
        lines.append(f"- Judge model: {run_config.get('judge_model') or 'disabled'}")
    if summary.get("by_agent"):
        lines.extend(["", "## Agents", ""])
        for agent, stats in summary["by_agent"].items():
            lines.append(f"### {agent}")
            lines.append(f"- Trials: {stats['total_trials']}")
            lines.append(f"- Objective success rate: {stats['objective_success_rate']:.2%}")
            lines.append(f"- Average composite: {stats['average_composite']:.1f}/100")
            lines.append(f"- S+A rate: {stats['s_or_a_rate']:.2%}")
            lines.append(f"- Avg turns: {stats['avg_turns']:.1f}")
            lines.append(f"- Avg duration: {stats['avg_duration_seconds']:.1f}s")
            lines.append(f"- Avg input tokens: {stats['average_input_tokens_total']:.1f}")
            lines.append(f"- Avg output tokens: {stats['average_output_tokens_total']:.1f}")
            lines.append(f"- Avg effective input tokens: {stats['average_effective_input_tokens']:.1f}")
            lines.append(f"- Avg cache read tokens: {stats['average_cache_read_tokens']:.1f}")
            lines.append(f"- Avg cache miss tokens: {stats['average_cache_miss_tokens']:.1f}")
            lines.append(f"- Avg cache creation tokens: {stats['average_cache_creation_tokens']:.1f}")
            lines.append(f"- Fallback rate: {stats['fallback_rate']:.2%}")
            lines.append("")
    lines.extend([
        "",
        "## Trials",
        "",
    ])
    for report in summary["reports"]:
        lines.append(f"### {report['agent_provider']} / {report['task_id']} / trial {report['trial_index']}")
        lines.append(f"- Objective success: {report['passed']}")
        lines.append(f"- Gate: {report['gate_score']:.1f}/40")
        lines.append(f"- Judge: {report['judge_score']:.1f}/60")
        lines.append(f"- Composite: {report['composite']:.1f}/100")
        lines.append(f"- Grade: {report['grade']}")
        lines.append(f"- Turns: {report['turns']}")
        lines.append(f"- Duration: {report['duration_seconds']:.1f}s")
        lines.append(f"- Tool calls: {report['tool_calls']}")
        lines.append(f"- Input tokens: {report['input_tokens_total']}")
        lines.append(f"- Output tokens: {report['output_tokens_total']}")
        lines.append(f"- Effective input tokens: {report['effective_input_tokens']}")
        lines.append(f"- Cache read tokens: {report['cache_read_tokens']}")
        lines.append(f"- Cache miss tokens: {report['cache_miss_tokens']}")
        lines.append(f"- Cache creation tokens: {report['cache_creation_tokens']}")
        lines.append(f"- Cache hit rate: {report['cache_hit_rate']:.2%}")
        for grader in report["graders"]:
            lines.append(
                f"- `{grader['name']}`: {'pass' if grader['passed'] else 'fail'} "
                f"({grader['score']:.2f}) - {grader['summary']}"
            )
        lines.append("")
    return "\n".join(lines)


def _find_grader(grader_results: list[GradeResult], name: str) -> GradeResult | None:
    for grader in grader_results:
        if grader.name == name:
            return grader
    return None


def _legacy_passed(grader_results: list[GradeResult]) -> bool:
    relevant = [item for item in grader_results if item.name != "llm_judge"]
    return all(item.passed for item in relevant) if relevant else False


def _gate_score(gate: GradeResult | None) -> float:
    if gate is None:
        return 0.0
    points = gate.details.get("gate_points", {})
    if points:
        return round(sum(float(value) for value in points.values()), 1)
    return round(gate.score * 40.0, 1)


def _gate_g1(gate: GradeResult | None) -> float:
    if gate is None:
        return 25.0
    points = gate.details.get("gate_points", {})
    return float(points.get("G1", gate.score * 25.0))


def _judge_score(judge: GradeResult | None) -> float:
    if judge is None:
        return 0.0
    if "judge_total" in judge.details:
        return round(float(judge.details["judge_total"]), 1)
    dimensions = judge.details.get("dimensions", {})
    total = 0.0
    seen = False
    for key in ("D1", "D2", "D3", "D4", "D5", "D6"):
        item = dimensions.get(key)
        if not isinstance(item, dict) or "score" not in item:
            continue
        seen = True
        total += max(0.0, min(float(item.get("score", 0.0)), 10.0))
    if seen:
        return round(total, 1)
    return round(judge.score * 60.0, 1)


def _grade_from_composite(composite: float, g1: float) -> str:
    grade = _base_grade(composite)
    if g1 <= 0:
        return "F"
    if g1 < 15.0 and grade in {"S", "A", "B"}:
        return "C"
    return grade


def _base_grade(composite: float) -> str:
    if composite >= 90.0:
        return "S"
    if composite >= 80.0:
        return "A"
    if composite >= 70.0:
        return "B"
    if composite >= 60.0:
        return "C"
    return "F"
