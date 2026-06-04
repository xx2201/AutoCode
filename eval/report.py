"""Report generation for evaluation runs."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .graders import GradeResult
from .schema import EvalTaskSpec


@dataclass
class TrialReport:
    task_id: str
    title: str
    trial_index: int
    passed: bool
    score: float
    graders: list[GradeResult]


def build_trial_report(spec: EvalTaskSpec, trial_index: int, grader_results: list[GradeResult]) -> TrialReport:
    score = sum(item.score for item in grader_results) / len(grader_results)
    passed = all(item.passed for item in grader_results)
    return TrialReport(
        task_id=spec.id,
        title=spec.title,
        trial_index=trial_index,
        passed=passed,
        score=score,
        graders=grader_results,
    )


def aggregate_reports(reports: list[TrialReport]) -> dict:
    if not reports:
        return {"total_trials": 0, "pass_rate": 0.0, "average_score": 0.0, "reports": []}
    passed = sum(1 for item in reports if item.passed)
    avg = sum(item.score for item in reports) / len(reports)
    return {
        "total_trials": len(reports),
        "pass_rate": passed / len(reports),
        "average_score": avg,
        "reports": [serialize_trial_report(item) for item in reports],
    }


def serialize_trial_report(report: TrialReport) -> dict:
    return {
        "task_id": report.task_id,
        "title": report.title,
        "trial_index": report.trial_index,
        "passed": report.passed,
        "score": report.score,
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
        f"- Pass rate: {summary['pass_rate']:.2%}",
        f"- Average score: {summary['average_score']:.2f}",
    ]
    run_config = summary.get("run_config")
    if run_config:
        lines.append(f"- Agent model: {run_config.get('agent_model', '')}")
        lines.append(f"- Judge model: {run_config.get('judge_model') or 'disabled'}")
    lines.extend([
        "",
        "## Trials",
        "",
    ])
    for report in summary["reports"]:
        lines.append(f"### {report['task_id']} / trial {report['trial_index']}")
        lines.append(f"- Passed: {report['passed']}")
        lines.append(f"- Score: {report['score']:.2f}")
        for grader in report["graders"]:
            lines.append(
                f"- `{grader['name']}`: {'pass' if grader['passed'] else 'fail'} "
                f"({grader['score']:.2f}) - {grader['summary']}"
            )
        lines.append("")
    return "\n".join(lines)
