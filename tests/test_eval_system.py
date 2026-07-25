from pathlib import Path
import os
import re
import subprocess
import threading
import time

from eval.graders import GradeResult, TrialArtifacts, VerificationCommandResult, VerificationResult, evaluate_cross_agent_trial, evaluate_trial
from eval.judge import JudgeConfig, LLMJudge
from eval.harness import (
    _build_claude_trace,
    _build_claude_eval_prompt,
    _claude_env,
    _resolve_claude_base_url,
    _collect_claude_logs,
    _extract_claude_actual_model,
    _extract_claude_last_assistant_text,
    _filter_platform_artifacts,
    _normalize_trace,
    _normalize_claude_prompt,
    _parse_claude_project_log,
    _platform_path,
    _run_captured_process,
    _write_claude_runner_script,
    _write_claude_workspace_settings,
    prepare_workspace,
)
from eval.loader import load_tasks
from eval.report import aggregate_reports, build_trial_report
from eval.runner import DEFAULT_EVAL_AGENT_MODEL, _load_eval_config, _resolve_eval_agent_model
from eval.schema import EvalTaskSpec
from autocode.config import Config


def _make_claude_spec(**overrides) -> EvalTaskSpec:
    payload = {
        "id": "claude-eval",
        "title": "claude-eval",
        "prompt": "Run `python -m unittest`.",
        "fixture": "none",
        "expectations": {
            "outcome": {
                "verification_commands": ["python -m unittest", "npm run build"],
            }
        },
    }
    payload.update(overrides)
    return EvalTaskSpec.from_dict(payload)


def test_eval_loader_reads_sample_tasks():
    tasks = load_tasks("eval/tasks")
    assert {task.id for task in tasks} == {
        "debug-billing-settlement-03",
        "debug-fusion-supply-fintech-04",
        "implement-spellbrigade-survivor-01",
        "multi-file-order-pipeline-01",
        "saga-warehouse-reconciliation-02",
    }


def test_prepare_workspace_copies_fixture(tmp_path):
    fixture = Path("eval/fixtures/debug-billing-settlement-03")
    dest = tmp_path / "workspace"
    prepare_workspace(fixture, dest)
    assert dest.joinpath("package.json").exists()
    assert dest.joinpath("test", "billing-settlement.test.ts").exists()
    assert dest.joinpath("src", "pipeline", "billing-orchestrator.ts").exists()
    assert dest.joinpath("RUNBOOK.md").exists()
    assert dest.joinpath("docs", "HANDBOOK.md").exists()


def test_billing_fixture_contains_nineteen_regression_probes(tmp_path):
    fixture = Path("eval/fixtures/debug-billing-settlement-03")
    dest = tmp_path / "workspace"
    prepare_workspace(fixture, dest)
    probe_file = dest / "test" / "billing-settlement.test.ts"
    content = probe_file.read_text(encoding="utf-8")
    assert len(re.findall(r"^\s*it\(", content, flags=re.MULTILINE)) == 19
    assert "billing pipeline applies discount before tax" in content


def test_billing_fixture_is_long_context_sized():
    src_root = Path("eval/fixtures/debug-billing-settlement-03/src")
    files = list(src_root.rglob("*.ts"))
    total_bytes = sum(len(path.read_text(encoding="utf-8").encode("utf-8")) for path in files)
    assert len(files) == 98
    assert total_bytes >= 450 * 1024


def test_eval_loader_reads_yaml_task(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    tasks_dir.joinpath("sample.yaml").write_text(
        "\n".join([
            "task_id: sample-yaml",
            "prompt: fix the bug",
            "repo: benchMark/repos/sample",
            "acceptance:",
            "  commands:",
            "    - npm test",
            "    - npm run build",
            "  files_must_exist:",
            "    - src/index.ts",
            "  files_must_not_change:",
            "    - test/sample.test.ts",
            "  allowed_change_globs:",
            "    - src/**/*.ts",
        ]),
        encoding="utf-8",
    )
    tasks = load_tasks(tasks_dir)
    assert len(tasks) == 1
    task = tasks[0]
    assert task.id == "sample-yaml"
    assert task.fixture == "benchMark/repos/sample"
    assert task.outcome.verification_commands == ["npm test", "npm run build"]
    assert task.outcome.files_must_exist == ["src/index.ts"]
    assert task.outcome.must_not_change_files == ["test/sample.test.ts"]
    assert task.safety.allowed_change_globs == ["src/**/*.ts"]


def test_eval_runner_uses_m27_by_default():
    assert DEFAULT_EVAL_AGENT_MODEL == "MiniMax-M2.7"
    assert _resolve_eval_agent_model(None) == "MiniMax-M2.7"
    assert _resolve_eval_agent_model("MiniMax-M3") == "MiniMax-M3"


def test_eval_runner_prefers_dotenv_snapshot_over_process_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTOCODE_MODEL", "MiniMax-M3")
    monkeypatch.setenv("AUTOCODE_BASE_URL", "https://mimimax.cn/v1")
    monkeypatch.setenv("AUTOCODE_API_KEY", "process-key")
    (tmp_path / ".env").write_text(
        "\n".join([
            "AUTOCODE_MODEL=deepseek-v4-flash",
            "AUTOCODE_BASE_URL=https://api.deepseek.com",
            "AUTOCODE_API_KEY=dotenv-key",
        ]),
        encoding="utf-8",
    )

    config = _load_eval_config()

    assert config.model == "deepseek-v4-flash"
    assert config.base_url == "https://api.deepseek.com"
    assert config.api_key == "dotenv-key"


def test_runtime_config_prefers_dotenv_snapshot_over_process_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTOCODE_MODEL", "MiniMax-M3")
    monkeypatch.setenv("AUTOCODE_BASE_URL", "https://mimimax.cn/v1")
    monkeypatch.setenv("AUTOCODE_API_KEY", "process-key")
    (tmp_path / ".env").write_text(
        "\n".join([
            "AUTOCODE_MODEL=gpt-5.5",
            "AUTOCODE_BASE_URL=https://pi-api-cn.macaron.xin",
            "AUTOCODE_API_KEY=dotenv-key",
        ]),
        encoding="utf-8",
    )

    config = Config.from_env()

    assert config.model == "gpt-5.5"
    assert config.base_url == "https://pi-api-cn.macaron.xin"
    assert config.api_key == "dotenv-key"


def test_eval_runner_prefers_eval_agent_settings_over_runtime_settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "\n".join([
            "AUTOCODE_MODEL=gpt-5.5",
            "AUTOCODE_BASE_URL=https://pi-api-cn.macaron.xin",
            "AUTOCODE_API_KEY=runtime-key",
            "AUTOCODE_EVAL_AGENT_MODEL=deepseek-v4-flash",
            "AUTOCODE_EVAL_AGENT_BASE_URL=https://api.deepseek.com",
            "AUTOCODE_EVAL_AGENT_API_KEY=eval-key",
        ]),
        encoding="utf-8",
    )

    config = _load_eval_config()

    assert config.model == "deepseek-v4-flash"
    assert config.base_url == "https://api.deepseek.com"
    assert config.api_key == "eval-key"


def test_claude_prompt_normalization_strips_backticks():
    prompt = "Run `python -m unittest` and inspect `.env.local`."
    assert _normalize_claude_prompt(prompt) == "Run python -m unittest and inspect .env.local."


def test_build_claude_eval_prompt_adds_cache_nonce():
    prompt = _build_claude_eval_prompt(_make_claude_spec(), "trial-1")
    assert prompt.startswith("[eval-run-id: trial-1]\n")
    assert "`" not in prompt
    assert "Do all work in the main agent. Do not use subagents." in prompt
    assert "On Windows, do not use Edit, MultiEdit, or Write for source changes." in prompt
    assert "Run these verification commands early to localize failures:" in prompt
    assert "Run python -m unittest." in prompt


def test_claude_workspace_settings_include_verification_commands(tmp_path):
    spec = EvalTaskSpec.from_dict({
        "id": "claude-settings",
        "title": "claude-settings",
        "prompt": "do work",
        "fixture": "none",
        "expectations": {
            "outcome": {
                "verification_commands": ["python -m unittest", "npm run build"]
            }
        }
    })
    _write_claude_workspace_settings(tmp_path, spec)
    payload = (tmp_path / ".claude" / "settings.local.json").read_text(encoding="utf-8")
    assert "Bash(python -m unittest)" in payload
    assert "Bash(npm run build)" in payload


def test_claude_runner_script_includes_model_override(tmp_path):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("fix bug", encoding="utf-8")
    script_path = _write_claude_runner_script(
        run_dir=tmp_path,
        prompt_file=prompt_file,
        executable=Path("C:/tools/claude.ps1"),
        workspace=tmp_path / "workspace",
        model_override="MiniMax-M3",
    )
    payload = script_path.read_text(encoding="utf-8")
    assert "MiniMax-M3" in payload
    assert "--disallowed-tools" in payload
    assert "'Edit' 'MultiEdit' 'Write'" in payload


def test_parse_claude_project_log_extracts_steps_and_tools(tmp_path):
    log = tmp_path / "session.jsonl"
    log.write_text(
        "\n".join([
            '{"type":"assistant","sessionId":"s1","message":{"id":"m1","usage":{"input_tokens":100,"output_tokens":10},"content":[{"type":"text","text":"hi"}]}}',
            '{"type":"assistant","sessionId":"s1","message":{"id":"m1","usage":{"input_tokens":100,"output_tokens":10},"content":[{"type":"tool_use","name":"Read"}]}}',
            '{"type":"assistant","sessionId":"s1","message":{"id":"m2","usage":{"input_tokens":120,"output_tokens":7},"content":[{"type":"tool_use","name":"Edit"},{"type":"tool_use","name":"Bash"}]}}',
        ]),
        encoding="utf-8",
    )
    stats = _parse_claude_project_log(log)
    assert stats["steps"] == 2
    assert stats["tool_calls"] == 3
    assert stats["prompt_tokens"] == 120
    assert stats["completion_tokens"] == 17
    assert stats["session_id"] == "s1"


def test_build_claude_trace_uses_total_input_and_cache_fields():
    payload = {
        "num_turns": 3,
        "session_id": "claude-session",
        "usage": {
            "input_tokens": 1200,
            "output_tokens": 300,
            "cache_creation_input_tokens": 400,
            "cache_read_input_tokens": 800,
        },
    }
    trace = _normalize_trace("claude_code", _build_claude_trace(payload, ""), payload)
    assert trace["input_tokens_total"] == 2400
    assert trace["output_tokens_total"] == 300
    assert trace["cache_miss_tokens"] == 1200
    assert trace["cache_creation_tokens"] == 400
    assert trace["cache_read_tokens"] == 800
    assert trace["effective_input_tokens"] == 1600
    assert trace["prompt_tokens"] == 2400


def test_extract_claude_actual_model_reads_first_assistant_model(tmp_path):
    log = tmp_path / "session.jsonl"
    log.write_text(
        "\n".join([
            '{"type":"user","message":{"content":[{"type":"text","text":"hi"}]}}',
            '{"type":"assistant","message":{"model":"deepseek-v4-flash","content":[{"type":"text","text":"done"}]}}',
            '{"type":"assistant","message":{"model":"MiniMax-M3","content":[{"type":"text","text":"later"}]}}',
        ]),
        encoding="utf-8",
    )
    assert _extract_claude_actual_model(str(log)) == "deepseek-v4-flash"


def test_claude_env_sanitizes_provider_env_and_applies_eval_config(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_MODEL", "MiniMax-M3")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://mimimax.cn/")
    monkeypatch.setenv("MINIMAX_API_KEY", "old-key")
    monkeypatch.setenv("CLAUDE_CODE_GIT_BASH_PATH", r"C:\Program Files\Git\bin\bash.exe")
    config = Config(
        model="deepseek-v4-flash",
        api_key="eval-key",
        base_url="https://api.deepseek.com",
    )

    env = _claude_env(tmp_path, config, model_override="deepseek-v4-flash")

    assert env["ANTHROPIC_MODEL"] == "deepseek-v4-flash"
    assert env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    assert env["ANTHROPIC_API_KEY"] == "eval-key"
    assert "MINIMAX_API_KEY" not in env
    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == "deepseek-v4-flash"
    assert env["CLAUDE_CODE_EFFORT_LEVEL"] == "max"
    assert env["CLAUDE_CODE_GIT_BASH_PATH"].endswith("bash.exe")


def test_resolve_claude_base_url_maps_deepseek_openai_endpoint_to_anthropic():
    assert _resolve_claude_base_url("https://api.deepseek.com") == "https://api.deepseek.com/anthropic"
    assert _resolve_claude_base_url("https://api.deepseek.com/v1") == "https://api.deepseek.com/v1/anthropic"
    assert _resolve_claude_base_url("https://api.deepseek.com/anthropic") == "https://api.deepseek.com/anthropic"


def test_claude_env_maps_deepseek_eval_endpoint_to_anthropic_and_sets_effort(tmp_path):
    config = Config(
        model="deepseek-v4-flash",
        api_key="eval-key",
        base_url="https://api.deepseek.com",
    )

    env = _claude_env(tmp_path, config, model_override="deepseek-v4-flash")

    assert env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == "deepseek-v4-flash"
    assert env["CLAUDE_CODE_EFFORT_LEVEL"] == "max"


def test_normalize_trace_preserves_autocode_cumulative_totals():
    trace = _normalize_trace(
        "autocode",
        {
            "prompt_tokens": 900,
            "completion_tokens": 120,
            "cache_read_tokens": 300,
            "cache_miss_tokens": 600,
        },
        {},
    )
    assert trace["input_tokens_total"] == 900
    assert trace["output_tokens_total"] == 120
    assert trace["effective_input_tokens"] == 600
    assert trace["cache_hit_rate"] == 0.3333


def test_platform_path_prefixes_windows_paths(tmp_path):
    value = _platform_path(tmp_path / "demo.jsonl")
    if os.name == "nt":
        assert value.startswith("\\\\?\\")
    else:
        assert value == str(tmp_path / "demo.jsonl")


def test_extract_claude_last_assistant_text_returns_last_text(tmp_path):
    log = tmp_path / "session.jsonl"
    log.write_text(
        "\n".join([
            '{"type":"assistant","message":{"content":[{"type":"text","text":"first"}]}}',
            '{"type":"user","message":{"content":[{"type":"text","text":"ignore"}]}}',
            '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read"},{"type":"text","text":"second"}]}}',
        ]),
        encoding="utf-8",
    )
    assert _extract_claude_last_assistant_text(str(log)) == "second"


def test_collect_claude_logs_prefers_main_project_log(tmp_path):
    projects_dir = tmp_path / ".claude" / "projects" / "demo"
    debug_dir = tmp_path / ".claude" / "debug"
    projects_dir.mkdir(parents=True)
    debug_dir.mkdir(parents=True)
    agent_log = projects_dir / "agent-helper.jsonl"
    main_log = projects_dir / "session-main.jsonl"
    agent_log.write_text("{}", encoding="utf-8")
    time.sleep(0.01)
    main_log.write_text("{}", encoding="utf-8")
    debug_log = debug_dir / "latest.txt"
    debug_log.write_text("debug", encoding="utf-8")

    logs = _collect_claude_logs(tmp_path, session_id="")

    assert logs["project_log"] == str(main_log)
    assert logs["debug_log"] == str(debug_log)


def test_collect_claude_logs_waits_for_project_log_when_session_known(tmp_path):
    projects_dir = tmp_path / ".claude" / "projects" / "demo"
    debug_dir = tmp_path / ".claude" / "debug"
    projects_dir.mkdir(parents=True)
    debug_dir.mkdir(parents=True)
    debug_log = debug_dir / "session-1.txt"
    debug_log.write_text("debug", encoding="utf-8")

    def _create_project_log() -> None:
        time.sleep(0.05)
        (projects_dir / "session-1.jsonl").write_text("{}", encoding="utf-8")

    worker = threading.Thread(target=_create_project_log)
    worker.start()
    logs = _collect_claude_logs(tmp_path, session_id="session-1")
    worker.join()

    assert logs["project_log"].endswith("session-1.jsonl")
    assert logs["debug_log"] == str(debug_log)


def test_filter_platform_artifacts_ignores_agent_runtime_artifacts():
    filtered = _filter_platform_artifacts(
        "icecoder",
        {
            ".autocode/PROJECT_MEMORY.md",
            ".claude/settings.local.json",
            ".iceCoder/memory.md",
            "data/sessions/default/bg/bg_1.log",
            "node_modules/.vite/vitest/hash/results.json",
            "app/service.py",
        },
    )
    assert filtered == {"app/service.py"}


def test_filter_platform_artifacts_keeps_real_dependency_changes():
    filtered = _filter_platform_artifacts(
        "claude_code",
        {
            "node_modules/.vite/vitest/hash/results.json",
            "node_modules/lodash/index.js",
            "src/index.ts",
        },
    )
    assert filtered == {"node_modules/lodash/index.js", "src/index.ts"}


def test_run_captured_process_kills_child_tree_on_timeout(tmp_path):
    flag = tmp_path / "child_flag.txt"
    child_script = tmp_path / "child.py"
    child_script.write_text(
        "\n".join([
            "import time",
            "from pathlib import Path",
            "time.sleep(3)",
            f"Path(r\"{flag}\").write_text(\"alive\", encoding=\"utf-8\")",
        ]),
        encoding="utf-8",
    )
    launcher_script = tmp_path / "launcher.py"
    launcher_script.write_text(
        "\n".join([
            "import subprocess",
            "import sys",
            "import time",
            f"subprocess.Popen([sys.executable, r\"{child_script}\"])",
            "time.sleep(30)",
        ]),
        encoding="utf-8",
    )
    try:
        _run_captured_process(
            ["python", str(launcher_script)],
            cwd=tmp_path,
            timeout=1,
        )
    except RuntimeError as exc:
        assert "timed out" in str(exc)
    else:
        raise AssertionError("expected timeout")
    time.sleep(4)
    assert not flag.exists()


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
                "must_use_tools": ["read", "edit_file"],
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
            {"event": "before_tool", "payload": {"tool_name": "read", "arguments": {"file_path": "main.py"}}},
            {"event": "before_tool", "payload": {"tool_name": "edit_file", "arguments": {"file_path": "main.py"}}},
        ],
        task_record=None,
        verification=VerificationResult(exit_code=0, output="ok"),
    )
    grader_results = evaluate_trial(spec, artifacts)
    report = build_trial_report(spec, 1, grader_results, artifacts=artifacts)
    summary = aggregate_reports([report])
    assert report.passed is True
    assert report.grade == "S"
    assert report.composite == 100.0
    assert summary["total_trials"] == 1
    assert summary["pass_rate"] == 1.0
    assert summary["average_composite"] == 100.0
    assert summary["by_agent"]["autocode"]["average_input_tokens_total"] == 10.0
    assert summary["by_agent"]["autocode"]["average_output_tokens_total"] == 5.0


def test_cross_agent_gate_uses_weighted_acceptance_and_scope(tmp_path):
    workspace = tmp_path
    workspace.joinpath("src").mkdir()
    workspace.joinpath("src/index.ts").write_text("export const ok = true;\n", encoding="utf-8")
    spec = EvalTaskSpec.from_dict({
        "task_id": "gate-task",
        "prompt": "fix project",
        "repo": "repo",
        "acceptance": {
            "commands": ["npm test", "npm run build"],
            "files_must_not_change": ["test/sample.test.ts"],
            "allowed_change_globs": ["src/**/*.ts"],
        },
        "expectations": {
            "judge": {"enabled": False}
        }
    })
    artifacts = TrialArtifacts(
        spec=spec,
        trial_index=1,
        workspace=workspace,
        final_response="done",
        trace={"modified_files": ["src/index.ts"]},
        audit=[],
        task_record=None,
        verification=VerificationResult(
            exit_code=0,
            output="OK",
            commands=[
                VerificationCommandResult(command="npm test", exit_code=0, output="OK"),
                VerificationCommandResult(command="npm run build", exit_code=0, output="build ok"),
            ],
        ),
    )
    results = evaluate_cross_agent_trial(spec, artifacts)
    gate = next(item for item in results if item.name == "gate")
    assert gate.passed is True
    assert gate.score == 1.0


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
    assert verdict.judge_total == 49.2
    assert verdict.summary == "good"


def test_llm_judge_prefers_dimension_sum_for_total():
    judge = LLMJudge(JudgeConfig(model="qwen-max", api_key="secret"))
    verdict = judge._parse_verdict(
        '{"dimensions":{"D1":{"score":10},"D2":{"score":9},"D3":{"score":8},"D4":{"score":10},"D5":{"score":8},"D6":{"score":7}},"one_line_verdict":"solid"}',
        min_score=0.7,
    )
    assert verdict.judge_total == 52.0
    assert verdict.score == round(52.0 / 60.0, 4)
    assert verdict.summary == "solid"


def test_llm_judge_consensus_averages_two_runs():
    judge = LLMJudge(JudgeConfig(model="qwen-max", api_key="secret"))
    verdicts = iter([
        judge._parse_verdict(
            '{"judge_total": 54, "dimensions":{"D1":{"score":10},"D2":{"score":9},"D3":{"score":8},"D4":{"score":10},"D5":{"score":9},"D6":{"score":8}}, "summary":"run1"}',
            min_score=0.7,
        ),
        judge._parse_verdict(
            '{"judge_total": 56, "dimensions":{"D1":{"score":10},"D2":{"score":10},"D3":{"score":8},"D4":{"score":10},"D5":{"score":9},"D6":{"score":8}}, "summary":"run2"}',
            min_score=0.7,
        ),
    ])
    judge._judge = lambda spec, artifacts: next(verdicts)  # type: ignore[method-assign]
    spec = EvalTaskSpec.from_dict({"id": "judge-task", "prompt": "do work", "fixture": "none"})
    artifacts = TrialArtifacts(spec=spec, trial_index=1, workspace=Path("."), final_response="", trace={}, audit=[], task_record=None, verification=None)
    result = judge.evaluate_consensus(spec, artifacts, repeats=2, arbitration_dimension_delta=3.0)
    assert result.details["judge_runs"] == 2
    assert result.details["arbitration_triggered"] is False
    assert result.details["judge_total"] == 55.0
    assert result.score == round(55.0 / 60.0, 4)


def test_llm_judge_consensus_triggers_arbitration_on_large_dimension_gap():
    judge = LLMJudge(JudgeConfig(model="qwen-max", api_key="secret"))
    verdicts = iter([
        judge._parse_verdict(
            '{"judge_total": 48, "dimensions":{"D1":{"score":10},"D2":{"score":10},"D3":{"score":4},"D4":{"score":8},"D5":{"score":8},"D6":{"score":8}}, "summary":"run1"}',
            min_score=0.7,
        ),
        judge._parse_verdict(
            '{"judge_total": 54, "dimensions":{"D1":{"score":10},"D2":{"score":10},"D3":{"score":8},"D4":{"score":9},"D5":{"score":8},"D6":{"score":9}}, "summary":"run2"}',
            min_score=0.7,
        ),
        judge._parse_verdict(
            '{"judge_total": 52, "dimensions":{"D1":{"score":10},"D2":{"score":10},"D3":{"score":7},"D4":{"score":8},"D5":{"score":8},"D6":{"score":9}}, "summary":"run3"}',
            min_score=0.7,
        ),
    ])
    judge._judge = lambda spec, artifacts: next(verdicts)  # type: ignore[method-assign]
    spec = EvalTaskSpec.from_dict({"id": "judge-task", "prompt": "do work", "fixture": "none"})
    artifacts = TrialArtifacts(spec=spec, trial_index=1, workspace=Path("."), final_response="", trace={}, audit=[], task_record=None, verification=None)
    result = judge.evaluate_consensus(spec, artifacts, repeats=2, arbitration_dimension_delta=3.0)
    assert result.details["judge_runs"] == 3
    assert result.details["arbitration_triggered"] is True
    assert result.details["judge_total"] == 51.3


def test_llm_judge_retries_empty_or_invalid_content_before_parsing():
    judge = LLMJudge(JudgeConfig(model="gpt-5.5", api_key="secret"))

    class _Resp:
        def __init__(self, content: str):
            self.content = content

    replies = iter([
        _Resp(""),
        _Resp("not-json"),
        _Resp('{"judge_total": 54, "dimensions":{"D1":{"score":10},"D2":{"score":9},"D3":{"score":9},"D4":{"score":9},"D5":{"score":9},"D6":{"score":8}}, "summary":"ok"}'),
    ])
    judge.llm.chat = lambda messages: next(replies)  # type: ignore[method-assign]
    spec = EvalTaskSpec.from_dict({"id": "judge-task", "prompt": "do work", "fixture": "none"})
    artifacts = TrialArtifacts(spec=spec, trial_index=1, workspace=Path("."), final_response="", trace={}, audit=[], task_record=None, verification=None)

    verdict = judge._judge(spec, artifacts)

    assert verdict.judge_total == 54.0
    assert verdict.passed is True


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


def test_aggregate_reports_groups_by_agent():
    spec = EvalTaskSpec.from_dict({
        "id": "synthetic",
        "title": "synthetic",
        "prompt": "do work",
        "fixture": "none",
    })
    report_a = build_trial_report(spec, 1, [], agent_provider="autocode")
    report_b = build_trial_report(spec, 1, [], agent_provider="claude_code")
    summary = aggregate_reports([report_a, report_b])
    assert summary["by_agent"]["autocode"]["total_trials"] == 1
    assert summary["by_agent"]["claude_code"]["total_trials"] == 1
    assert "average_composite" in summary["by_agent"]["autocode"]
    assert "average_input_tokens_total" in summary["by_agent"]["autocode"]


def test_build_trial_report_uses_gate_and_judge_composite(tmp_path):
    workspace = tmp_path
    spec = EvalTaskSpec.from_dict({
        "task_id": "judge-composite",
        "prompt": "fix project",
        "repo": "repo",
    })
    artifacts = TrialArtifacts(
        spec=spec,
        trial_index=1,
        workspace=workspace,
        final_response="done",
        trace={"llm_calls": 12, "tool_calls": 20, "duration_seconds": 30},
        audit=[],
        task_record=None,
        verification=None,
    )
    gate = GradeResult(
        name="gate",
        passed=True,
        score=1.0,
        summary="G1=25/25 G2=8/8 G3=4/4 G4=3/3",
        details={"gate_points": {"G1": 25.0, "G2": 8.0, "G3": 4.0, "G4": 3.0}},
    )
    judge = GradeResult(
        name="llm_judge",
        passed=True,
        score=52.0 / 60.0,
        summary="strong",
        details={"judge_total": 52.0, "dimensions": {}},
    )
    report = build_trial_report(spec, 1, [gate, judge], agent_provider="autocode", artifacts=artifacts)
    assert report.passed is True
    assert report.gate_score == 40.0
    assert report.judge_score == 52.0
    assert report.composite == 92.0
    assert report.grade == "S"
    assert report.turns == 12
    assert report.input_tokens_total == 0
