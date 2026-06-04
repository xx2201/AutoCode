"""CLI entrypoint for running agent evaluations."""

from __future__ import annotations

import argparse
from pathlib import Path

from corecoder.config import Config

from .graders import evaluate_trial
from .harness import default_output_dir, run_trial
from .judge import JudgeConfig, LLMJudge
from .loader import load_tasks
from .report import aggregate_reports, build_trial_report, write_report


def _parse_args():
    p = argparse.ArgumentParser(
        prog="python -m eval.runner",
        description="Independent evaluation harness for CoreCoder agents.",
    )
    p.add_argument("--task", action="append", help="Run a specific task id (repeatable)")
    p.add_argument("--tag", action="append", help="Run tasks that match a tag (repeatable)")
    p.add_argument("--tasks-dir", default="eval/tasks", help="Directory containing task JSON files")
    p.add_argument("--fixtures-dir", default="eval/fixtures", help="Directory containing fixture workspaces")
    p.add_argument("--output-dir", help="Directory for run outputs (default: eval/runs/<timestamp>)")
    p.add_argument("--list", action="store_true", help="List available tasks and exit")
    p.add_argument("--trials", type=int, help="Override trial count for all selected tasks")
    p.add_argument("--model", help="Override model name")
    p.add_argument("--judge-model", help="Override LLM judge model name")
    p.add_argument("--disable-llm-judge", action="store_true", help="Disable LLM judge even if configured")
    return p.parse_args()


def main():
    args = _parse_args()
    tasks = load_tasks(args.tasks_dir, task_ids=args.task, tags=args.tag)

    if args.list:
        for task in tasks:
            print(f"{task.id}: {task.title} [{', '.join(task.tags)}]")
        return

    if not tasks:
        raise SystemExit("No tasks selected.")

    config = Config.from_env()
    if not (args.model or config.model):
        raise SystemExit("No agent model configured. Set CORECODER_MODEL or pass --model.")
    if not config.api_key:
        raise SystemExit("No agent API key configured. Set CORECODER_API_KEY before running evals.")
    judge_config = None if args.disable_llm_judge else JudgeConfig.from_env(model_override=args.judge_model)
    judge = None if judge_config is None else LLMJudge(judge_config)

    fixtures_dir = Path(args.fixtures_dir)
    output_dir = Path(args.output_dir) if args.output_dir else default_output_dir(Path("eval"))
    reports = []

    for task in tasks:
        trial_count = args.trials or task.trials
        print(f"Running {task.id} ({trial_count} trial{'s' if trial_count != 1 else ''})")
        for trial_index in range(1, trial_count + 1):
            artifacts = run_trial(
                task,
                trial_index=trial_index,
                fixtures_dir=fixtures_dir,
                output_root=output_dir,
                config=config,
                model_override=args.model,
            )
            grader_results = evaluate_trial(task, artifacts)
            if judge is not None and task.judge.enabled:
                grader_results.append(judge.evaluate(task, artifacts))
            report = build_trial_report(task, trial_index, grader_results)
            reports.append(report)
            print(
                f"  trial {trial_index}: {'PASS' if report.passed else 'FAIL'} "
                f"(score {report.score:.2f})"
            )

    summary = aggregate_reports(reports)
    summary["run_config"] = {
        "agent_model": args.model or config.model,
        "judge_model": None if judge_config is None else judge_config.model,
    }
    write_report(output_dir, summary)
    print(f"\nSummary written to {output_dir}")
    print(f"Pass rate: {summary['pass_rate']:.2%}  Average score: {summary['average_score']:.2f}")


if __name__ == "__main__":
    main()
