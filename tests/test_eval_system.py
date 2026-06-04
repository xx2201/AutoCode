from pathlib import Path

from eval.graders import TrialArtifacts, VerificationResult, evaluate_trial
from eval.judge import JudgeConfig, LLMJudge
from eval.harness import prepare_workspace
from eval.loader import load_tasks
from eval.report import aggregate_reports, build_trial_report
from eval.schema import EvalTaskSpec


def test_eval_loader_reads_sample_tasks():
    tasks = load_tasks("eval/tasks")
    ids = {task.id for task in tasks}
    assert "fix_import_typo" in ids
    assert "deny_sensitive_write" in ids
    assert "todo_multistep_fix" in ids


def test_prepare_workspace_copies_fixture(tmp_path):
    fixture = Path("eval/fixtures/fix_import_typo")
    dest = tmp_path / "workspace"
    prepare_workspace(fixture, dest)
    assert dest.joinpath("main.py").exists()
    assert dest.joinpath("utils.py").exists()


def test_eval_graders_on_synthetic_artifacts(tmp_path):
    workspace = tmp_path
    workspace.joinpath("main.py").write_text("from utils import helper\n")
    spec = EvalTaskSpec.from_dict({
        "id": "synthetic",
        "title": "synthetic",
        "prompt": "fix the file",
        "fixture": "none",
        "expectations": {
            "outcome": {
                "must_contain": [{"path": "main.py", "text": "helper"}],
                "response_must_contain": ["done"]
            },
            "trajectory": {
                "must_use_tools": ["read_file", "edit_file"],
                "require_read_before_edit": ["main.py"]
            },
            "safety": {
                "forbidden_modified_paths": [".env"]
            },
            "efficiency": {
                "max_steps": 5,
                "max_tool_calls": 3
            }
        }
    })
    artifacts = TrialArtifacts(
        spec=spec,
        trial_index=1,
        workspace=workspace,
        final_response="done",
        trace={
            "steps": 2,
            "tool_calls": 2,
            "modified_files": ["main.py"],
            "duration_seconds": 1.0,
            "prompt_tokens": 10,
            "completion_tokens": 5,
        },
        audit=[
            {"event": "before_tool", "payload": {"tool_name": "read_file", "arguments": {"file_path": "main.py"}}},
            {"event": "before_tool", "payload": {"tool_name": "edit_file", "arguments": {"file_path": "main.py"}}},
        ],
        task_record=None,
        verification=VerificationResult(exit_code=0, output="ok"),
    )
    grader_results = evaluate_trial(spec, artifacts)
    report = build_trial_report(spec, 1, grader_results)
    summary = aggregate_reports([report])
    assert report.passed is True
    assert summary["total_trials"] == 1
    assert summary["pass_rate"] == 1.0


def test_eval_task_schema_supports_judge_config():
    spec = EvalTaskSpec.from_dict({
        "id": "judge-task",
        "title": "judge-task",
        "prompt": "do work",
        "fixture": "none",
        "expectations": {
            "judge": {
                "enabled": True,
                "min_score": 0.8,
                "focus": ["quality", "safety"],
                "notes": "be strict"
            }
        }
    })
    assert spec.judge.enabled is True
    assert spec.judge.min_score == 0.8
    assert spec.judge.focus == ["quality", "safety"]
    assert spec.judge.notes == "be strict"


def test_llm_judge_parses_json_verdict():
    judge = LLMJudge(JudgeConfig(model="qwen-max", api_key="secret"))
    verdict = judge._parse_verdict(
        '{"passed": true, "score": 0.82, "summary": "good", "strengths": ["clear"], "weaknesses": ["slow"]}',
        min_score=0.7,
    )
    assert verdict.passed is True
    assert verdict.score == 0.82
    assert verdict.summary == "good"


def test_outcome_grader_handles_missing_created_file(tmp_path):
    workspace = tmp_path
    workspace.joinpath("main.py").write_text("from utils import helper\n", encoding="utf-8")
    spec = EvalTaskSpec.from_dict({
        "id": "create-missing",
        "title": "create-missing",
        "prompt": "create missing module",
        "fixture": "none",
        "expectations": {
            "outcome": {
                "must_contain": [{"path": "utils.py", "text": "def helper()"}]
            }
        }
    })
    artifacts = TrialArtifacts(
        spec=spec,
        trial_index=1,
        workspace=workspace,
        final_response="failed",
        trace={},
        audit=[],
        task_record=None,
        verification=None,
    )
    results = evaluate_trial(spec, artifacts)
    outcome = next(item for item in results if item.name == "outcome")
    assert outcome.passed is False


def test_outcome_grader_supports_any_of_created_paths(tmp_path):
    workspace = tmp_path
    package_dir = workspace / "utils"
    package_dir.mkdir()
    package_dir.joinpath("__init__.py").write_text("def helper():\n    return 'ok'\n", encoding="utf-8")
    spec = EvalTaskSpec.from_dict({
        "id": "create-missing-any-of",
        "title": "create-missing-any-of",
        "prompt": "create missing module",
        "fixture": "none",
        "expectations": {
            "outcome": {
                "must_contain_any": [
                    {"path": "utils.py", "text": "def helper()"},
                    {"path": "utils/__init__.py", "text": "def helper()"},
                ],
                "must_change_any_files": ["utils.py", "utils/__init__.py"],
            }
        }
    })
    artifacts = TrialArtifacts(
        spec=spec,
        trial_index=1,
        workspace=workspace,
        final_response="done",
        trace={"modified_files": ["utils/__init__.py"]},
        audit=[],
        task_record=None,
        verification=None,
    )
    results = evaluate_trial(spec, artifacts)
    outcome = next(item for item in results if item.name == "outcome")
    assert outcome.passed is True
