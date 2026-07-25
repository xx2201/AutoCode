"""CLI entrypoint for running agent evaluations."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from autocode.config import Config

from .graders import evaluate_cross_agent_trial, evaluate_trial
from .harness import default_output_dir, run_trial
from .judge import JudgeConfig, LLMJudge
from .loader import load_tasks
from .report import aggregate_reports, build_trial_report, write_report

DEFAULT_EVAL_AGENT_MODEL = "MiniMax-M2.7"
DEFAULT_JUDGE_REPEATS = 2
DEFAULT_JUDGE_ARBITRATION_DELTA = 3.0


def _parse_args():
    p = argparse.ArgumentParser(
        prog="python -m eval.runner",
        description="Independent evaluation harness for AutoCode agents.",
    )
    p.add_argument("--task", action="append", help="Run a specific task id (repeatable)")
    p.add_argument("--tag", action="append", help="Run tasks that match a tag (repeatable)")
    p.add_argument("--tasks-dir", default="eval/tasks", help="Directory containing task JSON files")
    p.add_argument("--fixtures-dir", default="eval/fixtures", help="Directory containing fixture workspaces")
    p.add_argument("--output-dir", help="Directory for run outputs (default: eval/runs/<timestamp>)")
    p.add_argument("--list", action="store_true", help="List available tasks and exit")
    p.add_argument("--trials", type=int, help="Override trial count for all selected tasks")
    p.add_argument("--model", help="Override model name")
    p.add_argument("--agent", action="append", choices=["autocode", "claude_code", "icecoder"], help="Agent runtime to evaluate (repeatable)")
    p.add_argument("--icecoder-root", help="Path to the local iceCoder repository")
    p.add_argument("--judge-model", help="Override LLM judge model name")
    p.add_argument("--disable-llm-judge", action="store_true", help="Disable LLM judge even if configured")
    return p.parse_args()


def _resolve_eval_agent_model(args_model: str | None) -> str:
    return args_model or DEFAULT_EVAL_AGENT_MODEL


def _load_eval_dotenv() -> dict[str, str]:
    try:
        from dotenv import dotenv_values
    except ImportError:
        return {}

    cur = Path.cwd()
    home = Path.home()
    while True:
        candidate = cur / ".env"
        if candidate.exists():
            values = dotenv_values(candidate)
            return {
                key: value
                for key, value in values.items()
                if isinstance(key, str) and isinstance(value, str) and value
            }
        if cur == home or cur == cur.parent:
            return {}
        cur = cur.parent


def _resolve_eval_setting(snapshot: dict[str, str], key: str, default: str = "") -> str:
    return snapshot.get(key) or os.getenv(key, default)


def _resolve_eval_agent_setting(
    snapshot: dict[str, str],
    eval_key: str,
    runtime_key: str,
    default: str = "",
) -> str:
    return (
        snapshot.get(eval_key)
        or os.getenv(eval_key)
        or snapshot.get(runtime_key)
        or os.getenv(runtime_key, default)
    )


def _load_eval_config() -> Config:
    snapshot = _load_eval_dotenv()
    return Config(
        model=_resolve_eval_agent_setting(snapshot, "AUTOCODE_EVAL_AGENT_MODEL", "AUTOCODE_MODEL"),
        api_key=_resolve_eval_agent_setting(snapshot, "AUTOCODE_EVAL_AGENT_API_KEY", "AUTOCODE_API_KEY"),
        base_url=_resolve_eval_agent_setting(snapshot, "AUTOCODE_EVAL_AGENT_BASE_URL", "AUTOCODE_BASE_URL") or None,
        max_tokens=int(_resolve_eval_agent_setting(snapshot, "AUTOCODE_EVAL_AGENT_MAX_TOKENS", "AUTOCODE_MAX_TOKENS", "4096")),
        temperature=float(_resolve_eval_agent_setting(snapshot, "AUTOCODE_EVAL_AGENT_TEMPERATURE", "AUTOCODE_TEMPERATURE", "0")),
        max_context_tokens=int(_resolve_eval_agent_setting(snapshot, "AUTOCODE_EVAL_AGENT_MAX_CONTEXT", "AUTOCODE_MAX_CONTEXT", "1000000")),
        provider=_resolve_eval_agent_setting(snapshot, "AUTOCODE_EVAL_AGENT_PROVIDER", "AUTOCODE_PROVIDER", "openai"),
        workspace_root=_resolve_eval_agent_setting(snapshot, "AUTOCODE_EVAL_AGENT_WORKSPACE_ROOT", "AUTOCODE_WORKSPACE_ROOT", str(Path.cwd())),
        auto_approve=_resolve_eval_agent_setting(snapshot, "AUTOCODE_EVAL_AGENT_AUTO_APPROVE", "AUTOCODE_AUTO_APPROVE", "").lower() in {"1", "true", "yes", "on"},
        mcp_config_path=_resolve_eval_agent_setting(snapshot, "AUTOCODE_EVAL_AGENT_MCP_CONFIG", "AUTOCODE_MCP_CONFIG"),
    )


def main():
    args = _parse_args()
    tasks = load_tasks(args.tasks_dir, task_ids=args.task, tags=args.tag)

    if args.list:
        for task in tasks:
            print(f"{task.id}: {task.title} [{', '.join(task.tags)}]")
        return

    if not tasks:
        raise SystemExit("No tasks selected.")

    config = _load_eval_config()
    eval_agent_model = _resolve_eval_agent_model(args.model)
    if not config.api_key:
        raise SystemExit("No agent API key configured. Set AUTOCODE_API_KEY before running evals.")
    judge_config = None if args.disable_llm_judge else JudgeConfig.from_env(model_override=args.judge_model)
    judge = None if judge_config is None else LLMJudge(judge_config)
    agent_providers = args.agent or ["autocode"]
    cross_agent_mode = len(agent_providers) > 1 or any(agent != "autocode" for agent in agent_providers)

    fixtures_dir = Path(args.fixtures_dir)
    output_dir = Path(args.output_dir) if args.output_dir else default_output_dir(Path("eval"))
    reports = []

    for agent_provider in agent_providers:
        for task in tasks:
            trial_count = args.trials or task.trials
            print(
                f"Running {task.id} with {agent_provider} "
                f"({trial_count} trial{'s' if trial_count != 1 else ''})"
            )
            for trial_index in range(1, trial_count + 1):
                trial_output_root = output_dir / agent_provider
                artifacts = run_trial(
                    task,
                    trial_index=trial_index,
                    fixtures_dir=fixtures_dir,
                    output_root=trial_output_root,
                    config=config,
                    model_override=eval_agent_model,
                    agent_provider=agent_provider,
                    icecoder_root=args.icecoder_root,
                )
                grader_results = (
                    evaluate_cross_agent_trial(task, artifacts)
                    if cross_agent_mode
                    else evaluate_trial(task, artifacts)
                )
                if judge is not None and task.judge.enabled:
                    grader_results.append(
                        judge.evaluate_consensus(
                            task,
                            artifacts,
                            repeats=DEFAULT_JUDGE_REPEATS,
                            arbitration_dimension_delta=DEFAULT_JUDGE_ARBITRATION_DELTA,
                        )
                    )
                report = build_trial_report(
                    task,
                    trial_index,
                    grader_results,
                    agent_provider=agent_provider,
                    artifacts=artifacts,
                )
                reports.append(report)
                print(
                    f"  trial {trial_index}: {'PASS' if report.passed else 'FAIL'} "
                    f"(score {report.score:.2f})"
                )

    summary = aggregate_reports(reports)
    summary["run_config"] = {
        "agent_model": eval_agent_model,
        "agents": agent_providers,
        "cross_agent_mode": cross_agent_mode,
        "judge_model": None if judge_config is None else judge_config.model,
        "judge_repeats": 0 if judge_config is None else DEFAULT_JUDGE_REPEATS,
        "judge_arbitration_dimension_delta": (
            None if judge_config is None else DEFAULT_JUDGE_ARBITRATION_DELTA
        ),
    }
    write_report(output_dir, summary)
    print(f"\nSummary written to {output_dir}")
    print(f"Pass rate: {summary['pass_rate']:.2%}  Average score: {summary['average_score']:.2f}")


if __name__ == "__main__":
    main()

