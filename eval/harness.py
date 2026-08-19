"""Execution harness for running evaluation tasks against AutoCode."""

from __future__ import annotations

import json
import signal
import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
import os
import shutil as shutil_lib
from urllib.parse import urlparse, urlunparse

from autocode.agent import Agent
from autocode.config import Config
from autocode.llm import llm_class_for_provider
from autocode.state import SessionStore, load_events, load_trace
from autocode.state import checkpoint as checkpoint_module

from .graders import TrialArtifacts, VerificationCommandResult, VerificationResult
from .schema import EvalTaskSpec


class CapturedProcessError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        command: str | list[str],
        stdout: str,
        stderr: str,
        timed_out: bool = False,
        returncode: int | None = None,
    ) -> None:
        super().__init__(message)
        self.command = command
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.returncode = returncode


def create_llm(config: Config, model_override: str | None = None):
    llm_cls = llm_class_for_provider(config.provider)
    return llm_cls(
        model=model_override or config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
    )


@contextmanager
def patched_sessions_dir(path: Path):
    original = checkpoint_module.SESSIONS_DIR
    checkpoint_module.SESSIONS_DIR = path
    try:
        yield
    finally:
        checkpoint_module.SESSIONS_DIR = original


def prepare_workspace(fixture_dir: Path, destination: Path):
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(fixture_dir, destination)


def run_trial(
    spec: EvalTaskSpec,
    trial_index: int,
    fixtures_dir: Path,
    output_root: Path,
    config: Config,
    model_override: str | None = None,
    agent_provider: str = "autocode",
    icecoder_root: str | None = None,
) -> TrialArtifacts:
    run_dir = output_root / spec.id / f"trial_{trial_index}"
    workspace = run_dir / "workspace"
    task_artifacts_root = run_dir / "session_artifacts"
    fixture_root = resolve_fixture_root(spec, fixtures_dir)
    prepare_workspace(fixture_root, workspace)
    task_artifacts_root.mkdir(parents=True, exist_ok=True)

    if agent_provider == "autocode":
        llm = create_llm(config, model_override=model_override)
        with patched_sessions_dir(task_artifacts_root):
            agent = Agent(
                llm=llm,
                workspace_root=str(workspace),
                approval_policy="ask",
                max_rounds=spec.max_rounds,
            )
            started = time.time()
            response = agent.chat(spec.prompt, approval_handler=_approval_handler(spec))
            duration = time.time() - started

            if agent.task_state is None:
                raise RuntimeError("agent did not create a current task")

            trace = load_trace(agent.session_state.session_id) or {}
            trace.setdefault("duration_seconds", duration)
            trace = _normalize_trace(agent_provider, trace, {"trace": trace})
            audit = load_events(agent.session_state.session_id)
            task_record = SessionStore.load(agent.session_state.session_id)
            verification = run_verification(spec, workspace)
            payload = {
                "agent_provider": agent_provider,
                "session_id": agent.session_state.session_id,
                "task_id": agent.task_state.task_id,
                "response": response,
                "trace": trace,
                "task_record": task_record,
                "verification": _serialize_verification(verification),
            }
            (run_dir / "trial.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return TrialArtifacts(
                spec=spec,
                trial_index=trial_index,
                workspace=workspace,
                final_response=response,
                trace=trace,
                audit=audit,
                task_record=task_record,
                verification=verification,
            )

    started = time.time()
    if agent_provider == "claude_code":
        response, trace, raw_payload = _run_claude_code(
            spec,
            workspace,
            run_dir,
            config=config,
            model_override=model_override or config.model,
        )
    elif agent_provider == "icecoder":
        response, trace, raw_payload = _run_icecoder(
            spec,
            workspace,
            run_dir,
            config,
            icecoder_root=icecoder_root,
        )
    else:
        raise ValueError(f"unknown agent provider: {agent_provider}")
    duration = time.time() - started
    trace.setdefault("duration_seconds", duration)
    trace["modified_files"] = sorted(
        _filter_platform_artifacts(
            agent_provider,
            _diff_modified_files(fixture_root, workspace),
        )
    )
    trace = _normalize_trace(agent_provider, trace, raw_payload)
    verification = run_verification(spec, workspace)
    payload = {
        "agent_provider": agent_provider,
        "response": response,
        "trace": trace,
        "raw_agent_payload": raw_payload,
        "verification": _serialize_verification(verification),
    }
    (run_dir / "trial.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return TrialArtifacts(
        spec=spec,
        trial_index=trial_index,
        workspace=workspace,
        final_response=response,
        trace=trace,
        audit=[],
        task_record=None,
        verification=verification,
    )


def run_verification(spec: EvalTaskSpec, workspace: Path) -> VerificationResult | None:
    commands = list(spec.outcome.verification_commands)
    if not commands and spec.outcome.verification_command:
        commands = [spec.outcome.verification_command]
    if not commands:
        return None
    results: list[VerificationCommandResult] = []
    combined_parts: list[str] = []
    any_failed = False
    for command in commands:
        proc = _run_captured_process(
            command,
            cwd=workspace,
            shell=True,
        )
        output = proc.stdout
        if proc.stderr:
            output += f"\n[stderr]\n{proc.stderr}"
        output = output.strip()
        results.append(VerificationCommandResult(command=command, exit_code=proc.returncode, output=output))
        combined_parts.append(f"$ {command}\n{output}".strip())
        any_failed = any_failed or proc.returncode != 0
    return VerificationResult(
        exit_code=1 if any_failed else 0,
        output="\n\n".join(combined_parts).strip(),
        commands=results,
    )


def default_output_dir(base: Path) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return base / "runs" / stamp


def _approval_handler(spec: EvalTaskSpec):
    if spec.approval_mode == "reject_all":
        return lambda pending: False
    return lambda pending: True


def resolve_fixture_root(spec: EvalTaskSpec, fixtures_dir: Path) -> Path:
    candidate = Path(spec.fixture)
    if candidate.is_absolute() and candidate.exists():
        return candidate
    direct = fixtures_dir / spec.fixture
    if direct.exists():
        return direct
    if spec.source_path:
        source_dir = Path(spec.source_path).resolve().parent
        relative = (source_dir / spec.fixture).resolve()
        if relative.exists():
            return relative
        bench_variant = source_dir.parent / "repos" / Path(spec.fixture).name
        if bench_variant.exists():
            return bench_variant.resolve()
        alt_variant = source_dir.parent / "benchMark" / "repos" / Path(spec.fixture).name
        if alt_variant.exists():
            return alt_variant.resolve()
    raise FileNotFoundError(f"fixture/repo not found for task {spec.id}: {spec.fixture}")


def _serialize_verification(verification: VerificationResult | None) -> dict | None:
    if verification is None:
        return None
    return {
        "exit_code": verification.exit_code,
        "output": verification.output,
        "commands": [
            {"command": item.command, "exit_code": item.exit_code, "output": item.output}
            for item in verification.commands
        ],
    }


def _run_claude_code(
    spec: EvalTaskSpec,
    workspace: Path,
    run_dir: Path,
    config: Config,
    model_override: str | None = None,
) -> tuple[str, dict, dict]:
    executable = _resolve_claude_powershell_script()
    prompt = _build_claude_eval_prompt(spec, _claude_cache_nonce(run_dir))
    sandbox_home = _prepare_claude_home(run_dir)
    _write_claude_workspace_settings(workspace, spec)
    prompt_file = run_dir / "claude_prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8")
    runner_script = _write_claude_runner_script(
        run_dir=run_dir,
        prompt_file=prompt_file,
        executable=Path(executable),
        workspace=workspace,
        model_override=model_override,
    )
    command = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(runner_script),
    ]
    payload: dict = {}
    process_error: CapturedProcessError | None = None
    try:
        proc = _run_captured_process(
            command,
            cwd=workspace,
            env=_claude_env(sandbox_home, config, model_override=model_override),
            timeout=max(spec.max_rounds * 60, 600),
        )
        payload = _parse_json_output(proc.stdout)
        if proc.returncode != 0:
            raise CapturedProcessError(
                proc.stderr.strip() or proc.stdout.strip() or "claude code run failed",
                command=command,
                stdout=proc.stdout,
                stderr=proc.stderr,
                returncode=proc.returncode,
            )
    except CapturedProcessError as exc:
        process_error = exc
        try:
            payload = _parse_json_output(exc.stdout)
        except json.JSONDecodeError:
            payload = {}
    claude_logs = _collect_claude_logs(sandbox_home, str(payload.get("session_id", "")))
    project_log = claude_logs.get("project_log", "")
    actual_model = _extract_claude_actual_model(project_log)
    payload["requested_model"] = model_override or ""
    payload["actual_model"] = actual_model
    payload["model_mismatch"] = bool(actual_model and model_override and actual_model != model_override)
    trace = _build_claude_trace(payload, project_log)
    payload["sandbox_home"] = str(sandbox_home)
    payload["workspace_settings"] = str(workspace / ".claude" / "settings.local.json")
    payload["project_log"] = project_log
    payload["debug_log"] = claude_logs.get("debug_log", "")
    payload["runner_script"] = str(runner_script)
    if process_error is not None:
        payload["process_error"] = str(process_error)
        payload["timed_out"] = process_error.timed_out
        if process_error.returncode is not None:
            payload["returncode"] = process_error.returncode
    response = str(payload.get("result", "")).strip()
    if not response:
        response = _extract_claude_last_assistant_text(project_log)
    if process_error is not None and not response:
        response = str(process_error)
    return response, trace, payload


def _build_claude_trace(payload: dict, project_log: str) -> dict:
    trace = {
        "steps": payload.get("num_turns", 0),
        "llm_calls": payload.get("num_turns", 0),
        "tool_calls": 0,
        "prompt_tokens": payload.get("usage", {}).get("input_tokens", 0),
        "completion_tokens": payload.get("usage", {}).get("output_tokens", 0),
        "cache_read_tokens": payload.get("usage", {}).get("cache_read_input_tokens", 0),
        "cache_creation_tokens": payload.get("usage", {}).get("cache_creation_input_tokens", 0),
        "session_id": payload.get("session_id", ""),
        "requested_model": payload.get("requested_model", ""),
        "actual_model": payload.get("actual_model", ""),
        "model_mismatch": bool(payload.get("model_mismatch", False)),
    }
    if not project_log:
        return trace
    stats = _parse_claude_project_log(Path(project_log))
    trace["steps"] = max(trace["steps"], stats["steps"])
    trace["llm_calls"] = max(trace["llm_calls"], stats["steps"])
    trace["tool_calls"] = stats["tool_calls"]
    trace["prompt_tokens"] = max(trace["prompt_tokens"], stats["prompt_tokens"])
    trace["completion_tokens"] = max(trace["completion_tokens"], stats["completion_tokens"])
    if stats["session_id"]:
        trace["session_id"] = stats["session_id"]
    return trace


def _run_icecoder(
    spec: EvalTaskSpec,
    workspace: Path,
    run_dir: Path,
    config: Config,
    icecoder_root: str | None = None,
) -> tuple[str, dict, dict]:
    root = Path(icecoder_root or "G:/mycode/iceCoder").resolve()
    data_dir = _ensure_icecoder_config(run_dir / "icecoder_data", config)
    tsx_cli = root / "node_modules" / "tsx" / "dist" / "cli.mjs"
    if not tsx_cli.exists():
        raise FileNotFoundError(f"iceCoder dependencies missing: {tsx_cli}")
    command = [
        "node",
        str(tsx_cli),
        str(root / "src" / "cli" / "index.ts"),
        "run",
        spec.prompt,
        "--json",
        "--max-rounds",
        str(spec.max_rounds),
    ]
    proc = _run_captured_process(
        command,
        cwd=workspace,
        env={**os.environ, "ICE_DATA_DIR": str(data_dir)},
        timeout=max(spec.max_rounds * 30, 300),
    )
    payload = _parse_json_output(proc.stdout)
    if proc.returncode != 0 or not payload.get("success", False):
        raise RuntimeError(payload.get("error") or proc.stderr.strip() or proc.stdout.strip() or "iceCoder run failed")
    tokens = payload.get("tokens", {})
    trace = {
        "steps": payload.get("rounds", 0),
        "llm_calls": payload.get("rounds", 0),
        "tool_calls": payload.get("toolCalls", 0),
        "prompt_tokens": tokens.get("input", 0),
        "completion_tokens": tokens.get("output", 0),
        "cache_read_tokens": tokens.get("cacheRead", 0),
        "cache_miss_tokens": tokens.get("cacheMiss", 0),
        "cache_creation_tokens": tokens.get("cacheCreation", 0),
        "stop_reason": payload.get("stopReason", ""),
    }
    return str(payload.get("content", "")).strip(), trace, payload


def _normalize_trace(agent_provider: str, trace: dict, raw_payload: dict | None) -> dict:
    normalized = dict(trace or {})
    payload = raw_payload or {}

    if agent_provider == "claude_code":
        usage = payload.get("usage", {})
        input_tokens = _to_int(usage.get("input_tokens"))
        output_tokens = _to_int(usage.get("output_tokens"))
        cache_read = _to_int(usage.get("cache_read_input_tokens"))
        cache_creation = _to_int(usage.get("cache_creation_input_tokens"))
        total_input = input_tokens + cache_read + cache_creation
        normalized["cache_miss_tokens"] = input_tokens
        normalized["cache_read_tokens"] = cache_read
        normalized["cache_creation_tokens"] = cache_creation
        normalized["input_tokens_total"] = total_input
        normalized["output_tokens_total"] = output_tokens
        normalized["token_accounting"] = {
            "input_tokens_total": "usage.input_tokens + usage.cache_read_input_tokens + usage.cache_creation_input_tokens",
            "cache_miss_tokens": "usage.input_tokens",
            "cache_read_tokens": "usage.cache_read_input_tokens",
            "cache_creation_tokens": "usage.cache_creation_input_tokens",
            "source": "claude_cli_json",
        }
    elif agent_provider == "icecoder":
        tokens = payload.get("tokens", {})
        input_tokens = _coalesce_int(tokens.get("input"), normalized.get("prompt_tokens"))
        output_tokens = _coalesce_int(tokens.get("output"), normalized.get("completion_tokens"))
        cache_read = _to_int(tokens.get("cacheRead"))
        cache_miss = _to_int(tokens.get("cacheMiss"))
        cache_creation = _to_int(tokens.get("cacheCreation"))
        normalized["cache_read_tokens"] = cache_read
        normalized["cache_miss_tokens"] = cache_miss
        normalized["cache_creation_tokens"] = cache_creation
        normalized["input_tokens_total"] = input_tokens
        normalized["output_tokens_total"] = output_tokens
        normalized["token_accounting"] = {
            "input_tokens_total": "payload.tokens.input",
            "cache_miss_tokens": "payload.tokens.cacheMiss if present",
            "cache_read_tokens": "payload.tokens.cacheRead if present",
            "cache_creation_tokens": "payload.tokens.cacheCreation if present",
            "source": "icecoder_run_json",
        }
    else:
        input_tokens = _coalesce_int(normalized.get("input_tokens_total"), normalized.get("prompt_tokens"))
        output_tokens = _coalesce_int(normalized.get("output_tokens_total"), normalized.get("completion_tokens"))
        cache_read = _to_int(normalized.get("cache_read_tokens"))
        cache_miss = _to_int(normalized.get("cache_miss_tokens"))
        cache_creation = _to_int(normalized.get("cache_creation_tokens"))
        normalized["cache_read_tokens"] = cache_read
        normalized["cache_miss_tokens"] = cache_miss
        normalized["cache_creation_tokens"] = cache_creation
        normalized["input_tokens_total"] = input_tokens
        normalized["output_tokens_total"] = output_tokens
        normalized["token_accounting"] = {
            "input_tokens_total": "runtime trace cumulative prompt_tokens",
            "cache_miss_tokens": "runtime trace cumulative cache_miss_tokens",
            "cache_read_tokens": "runtime trace cumulative cache_read_tokens",
            "cache_creation_tokens": "not currently emitted by runtime trace",
            "source": "autocode_runtime_trace",
        }

    normalized["prompt_tokens"] = normalized.get("input_tokens_total", 0)
    normalized["completion_tokens"] = normalized.get("output_tokens_total", 0)

    cache_read = _to_int(normalized.get("cache_read_tokens"))
    cache_miss = _to_int(normalized.get("cache_miss_tokens"))
    cache_creation = _to_int(normalized.get("cache_creation_tokens"))
    if cache_read or cache_miss:
        denominator = cache_read + cache_miss
        normalized["cache_hit_rate"] = round(cache_read / denominator, 4) if denominator else 0.0
    else:
        normalized["cache_hit_rate"] = 0.0
    normalized["effective_input_tokens"] = cache_miss + cache_creation if (cache_miss or cache_creation) else normalized["input_tokens_total"]
    return normalized


def _to_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _coalesce_int(*values) -> int:
    for value in values:
        parsed = _to_int(value)
        if parsed:
            return parsed
    return 0


def _ensure_icecoder_config(data_dir: Path, config: Config) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path = data_dir / "config.json"
    payload = {
        "providers": [
            {
                "id": config.model or "eval-provider",
                "apiUrl": config.base_url,
                "apiKey": config.api_key,
                "modelName": config.model,
                "parameters": {"temperature": config.temperature},
                "isDefault": True,
                "supportsVision": True,
                "maxContextTokens": config.max_context_tokens,
            }
        ]
    }
    config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return data_dir


def _parse_json_output(stdout: str) -> dict:
    text = stdout.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.rfind("\n{")
        if start != -1:
            start += 1
        else:
            start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


def _resolve_claude_executable() -> str:
    for candidate in ("claude.cmd", "claude", "claude.ps1"):
        resolved = shutil_lib.which(candidate)
        if resolved:
            return resolved
    raise FileNotFoundError("Claude Code executable not found in PATH")


def _resolve_claude_powershell_script() -> str:
    executable = Path(_resolve_claude_executable())
    if executable.suffix.lower() == ".ps1":
        return str(executable)
    ps1 = executable.with_suffix(".ps1")
    if ps1.exists():
        return str(ps1)
    raise FileNotFoundError(f"Claude PowerShell wrapper not found next to {executable}")


def _normalize_claude_prompt(prompt: str) -> str:
    return prompt.replace("`", "")


def _build_claude_eval_prompt(spec: EvalTaskSpec, cache_nonce: str) -> str:
    normalized = _normalize_claude_prompt(spec.prompt)
    commands = list(spec.outcome.verification_commands)
    if not commands and spec.outcome.verification_command:
        commands = [spec.outcome.verification_command]
    protocol = [
        f"[eval-run-id: {cache_nonce}]",
        "Execution protocol:",
        "- Do all work in the main agent. Do not use subagents.",
        "- Read only the files needed to localize and fix the task.",
        "- If a file is ordinary app, test, or config code, any malware classification should be one short sentence at most.",
        "- Prefer workspace-relative paths for read operations whenever possible.",
        "- On Windows, do not use Edit, MultiEdit, or Write for source changes.",
        "- Use Bash with a short Python script or equivalent deterministic patch command for file modifications.",
        "- Keep edits minimal and inside the workspace.",
    ]
    if commands:
        if len(commands) == 1:
            protocol.append(f"- Run this verification command early to localize failures: {commands[0]}")
        else:
            protocol.append(f"- Run these verification commands early to localize failures: {' ; '.join(commands)}")
    return "\n".join(protocol) + "\n\n" + normalized


def _prepare_claude_home(run_dir: Path) -> Path:
    sandbox_home = run_dir / "claude_home"
    (sandbox_home / ".claude").mkdir(parents=True, exist_ok=True)
    (sandbox_home / "AppData" / "Roaming").mkdir(parents=True, exist_ok=True)
    (sandbox_home / "AppData" / "Local").mkdir(parents=True, exist_ok=True)
    return sandbox_home


def _claude_env(sandbox_home: Path, config: Config, model_override: str | None = None) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not _is_claude_provider_env(key)
    }
    target_model = model_override or config.model
    if config.api_key:
        env["ANTHROPIC_API_KEY"] = config.api_key
        env["ANTHROPIC_AUTH_TOKEN"] = config.api_key
    if config.base_url:
        env["ANTHROPIC_BASE_URL"] = _resolve_claude_base_url(config.base_url)
    if target_model:
        env["ANTHROPIC_MODEL"] = target_model
        env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = target_model
        env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = target_model
        env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = target_model
        env["ANTHROPIC_SMALL_FAST_MODEL"] = target_model
        if _is_deepseek_model(target_model):
            env["CLAUDE_CODE_SUBAGENT_MODEL"] = target_model
            env["CLAUDE_CODE_EFFORT_LEVEL"] = "max"
    env.update({
        "HOME": str(sandbox_home),
        "USERPROFILE": str(sandbox_home),
        "APPDATA": str(sandbox_home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(sandbox_home / "AppData" / "Local"),
    })
    return env


def _is_claude_provider_env(name: str) -> bool:
    prefixes = (
        "ANTHROPIC_",
        "MINIMAX_",
        "OPENAI_",
        "DEEPSEEK_",
    )
    return name.upper().startswith(prefixes)


def _resolve_claude_base_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    if host == "api.deepseek.com" and not path.endswith("/anthropic"):
        return urlunparse(parsed._replace(path=f"{path}/anthropic" if path else "/anthropic"))
    return base_url


def _is_deepseek_model(model: str) -> bool:
    normalized = (model or "").lower()
    return normalized.startswith("deepseek-") or normalized.startswith("deepseek/")


def _write_claude_workspace_settings(workspace: Path, spec: EvalTaskSpec) -> None:
    settings_dir = workspace / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    commands = list(spec.outcome.verification_commands)
    if not commands and spec.outcome.verification_command:
        commands = [spec.outcome.verification_command]
    allow = [f"Bash({command})" for command in commands]
    payload = {
        "permissions": {
            "allow": allow,
            "deny": [],
            "ask": [],
        }
    }
    (settings_dir / "settings.local.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _write_claude_runner_script(
    *,
    run_dir: Path,
    prompt_file: Path,
    executable: Path,
    workspace: Path,
    model_override: str | None,
) -> Path:
    args = [
        "-p",
        "--output-format",
        "json",
        "--input-format",
        "text",
        "--permission-mode",
        "bypassPermissions",
        "--disallowed-tools",
        "Edit",
        "MultiEdit",
        "Write",
        "--add-dir",
        str(workspace),
    ]
    if model_override:
        args.extend(["--model", model_override])
    quoted_args = " ".join(_powershell_quote(arg) for arg in args)
    script = "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"Get-Content -Raw -LiteralPath {_powershell_quote(str(prompt_file))} | "
        f"& {_powershell_quote(str(executable))} {quoted_args}",
    ])
    script_path = run_dir / "run_claude_eval.ps1"
    script_path.write_text(script, encoding="utf-8")
    return script_path


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _claude_cache_nonce(run_dir: Path) -> str:
    return run_dir.resolve().as_posix()


def _collect_claude_logs(sandbox_home: Path, session_id: str) -> dict[str, str]:
    claude_dir = sandbox_home / ".claude"
    for attempt in range(20):
        project_logs = _sorted_existing_paths(claude_dir.glob("projects/**/*.jsonl"))
        debug_logs = _sorted_existing_paths(claude_dir.glob("debug/*.txt"))
        selected_project = ""
        selected_debug = ""
        if session_id:
            for path in project_logs:
                if path.name == f"{session_id}.jsonl":
                    selected_project = str(path)
                    break
            for path in debug_logs:
                if path.name == f"{session_id}.txt":
                    selected_debug = str(path)
                    break
        if not selected_project and project_logs:
            main_logs = [path for path in project_logs if not path.name.startswith("agent-")]
            selected_project = str((main_logs or project_logs)[-1])
        if not selected_debug and debug_logs:
            selected_debug = str(debug_logs[-1])
        if selected_project:
            return {
                "project_log": selected_project,
                "debug_log": selected_debug,
            }
        if not session_id and selected_debug:
            return {
                "project_log": "",
                "debug_log": selected_debug,
            }
        if session_id and selected_debug and attempt == 19:
            return {
                "project_log": "",
                "debug_log": selected_debug,
            }
        time.sleep(0.2)
    return {"project_log": "", "debug_log": ""}


def _sorted_existing_paths(paths) -> list[Path]:
    existing: list[tuple[float, Path]] = []
    for path in paths:
        try:
            existing.append((os.stat(_platform_path(path)).st_mtime, path))
        except FileNotFoundError:
            continue
    existing.sort(key=lambda item: item[0])
    return [path for _, path in existing]


def _parse_claude_project_log(path: Path) -> dict[str, int | str]:
    message_ids: dict[str, tuple[int, int]] = {}
    tool_calls = 0
    session_id = ""
    for raw_line in _read_text(path).splitlines():
        if not raw_line.strip():
            continue
        item = json.loads(raw_line)
        session_id = session_id or str(item.get("sessionId", ""))
        if item.get("type") != "assistant":
            continue
        message = item.get("message", {})
        message_id = str(message.get("id", ""))
        usage = message.get("usage", {})
        input_tokens = int(usage.get("input_tokens", 0))
        output_tokens = int(usage.get("output_tokens", 0))
        if message_id and message_id not in message_ids:
            message_ids[message_id] = (input_tokens, output_tokens)
        for content in message.get("content", []):
            if content.get("type") == "tool_use":
                tool_calls += 1
    prompt_tokens = max((tokens[0] for tokens in message_ids.values()), default=0)
    completion_tokens = sum(tokens[1] for tokens in message_ids.values())
    return {
        "steps": len(message_ids),
        "tool_calls": tool_calls,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "session_id": session_id,
    }


def _extract_claude_actual_model(project_log: str) -> str:
    if not project_log:
        return ""
    path = Path(project_log)
    for raw_line in _read_text(path).splitlines():
        if not raw_line.strip():
            continue
        item = json.loads(raw_line)
        if item.get("type") != "assistant":
            continue
        message = item.get("message", {})
        model = str(message.get("model", "")).strip()
        if model:
            return model
    return ""


def _extract_claude_last_assistant_text(project_log: str) -> str:
    if not project_log:
        return ""
    last_text = ""
    path = Path(project_log)
    for raw_line in _read_text(path).splitlines():
        if not raw_line.strip():
            continue
        item = json.loads(raw_line)
        if item.get("type") != "assistant":
            continue
        message = item.get("message", {})
        for content in message.get("content", []):
            if content.get("type") == "text":
                text = str(content.get("text", "")).strip()
                if text:
                    last_text = text
    return last_text


def _run_captured_process(
    command: str | list[str],
    *,
    cwd: Path,
    timeout: int | float | None = None,
    shell: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        shell=shell,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_tree(proc)
        stdout, stderr = proc.communicate()
        raise CapturedProcessError(
            f"command timed out after {timeout}s: {command}\nstdout:\n{stdout}\nstderr:\n{stderr}"
            ,
            command=command,
            stdout=stdout,
            stderr=stderr,
            timed_out=True,
        ) from exc
    return subprocess.CompletedProcess(
        args=command,
        returncode=proc.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _terminate_process_tree(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return
    proc.send_signal(signal.SIGTERM)


def _read_text(path: Path) -> str:
    with open(_platform_path(path), "r", encoding="utf-8") as handle:
        return handle.read()


def _platform_path(path: Path) -> str:
    raw = str(path)
    if os.name != "nt" or raw.startswith("\\\\?\\"):
        return raw
    if raw.startswith("\\\\"):
        return "\\\\?\\UNC\\" + raw[2:]
    return "\\\\?\\" + str(path.resolve(strict=False))


def _diff_modified_files(source: Path, workspace: Path) -> set[str]:
    changed: set[str] = set()
    source_files = {path.relative_to(source).as_posix(): path for path in source.rglob("*") if path.is_file()}
    workspace_files = {path.relative_to(workspace).as_posix(): path for path in workspace.rglob("*") if path.is_file()}
    for rel_path in sorted(set(source_files) | set(workspace_files)):
        left = source_files.get(rel_path)
        right = workspace_files.get(rel_path)
        if left is None or right is None:
            changed.add(rel_path)
            continue
        if left.read_bytes() != right.read_bytes():
            changed.add(rel_path)
    return changed


_PLATFORM_ARTIFACT_PREFIXES = (
    ".autocode/",
    ".claude/",
    ".iceCoder/",
    "data/sessions/",
    "node_modules/.vite/vitest/",
)


def _filter_platform_artifacts(agent_provider: str, modified_files: set[str]) -> set[str]:
    del agent_provider
    return {
        path
        for path in modified_files
        if not path.startswith(_PLATFORM_ARTIFACT_PREFIXES)
    }

