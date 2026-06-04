"""Execution harness for running evaluation tasks against CoreCoder."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from corecoder.agent import Agent
from corecoder.config import Config
from corecoder.journal import load_events
from corecoder.llm import LLM, LiteLLM
from corecoder.tasks import TaskStore
from corecoder.trace import load_trace
from corecoder import checkpoint as checkpoint_module

from .graders import TrialArtifacts, VerificationResult
from .schema import EvalTaskSpec


def create_llm(config: Config, model_override: str | None = None):
    llm_cls = LiteLLM if config.provider == "litellm" else LLM
    return llm_cls(
        model=model_override or config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
    )


@contextmanager
def patched_tasks_dir(path: Path):
    original = checkpoint_module.TASKS_DIR
    checkpoint_module.TASKS_DIR = path
    try:
        yield
    finally:
        checkpoint_module.TASKS_DIR = original


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
) -> TrialArtifacts:
    run_dir = output_root / spec.id / f"trial_{trial_index}"
    workspace = run_dir / "workspace"
    task_artifacts_root = run_dir / "task_artifacts"
    prepare_workspace(fixtures_dir / spec.fixture, workspace)
    task_artifacts_root.mkdir(parents=True, exist_ok=True)

    llm = create_llm(config, model_override=model_override)
    with patched_tasks_dir(task_artifacts_root):
        agent = Agent(
            llm=llm,
            workspace_root=str(workspace),
            auto_approve=spec.auto_approve,
            max_rounds=spec.max_rounds,
        )
        started = time.time()
        response = agent.chat(spec.prompt, approval_handler=_approval_handler(spec))
        duration = time.time() - started

        if agent.task_state is None:
            raise RuntimeError("agent did not create a task state")

        trace = load_trace(agent.task_state.task_id) or {}
        trace.setdefault("duration_seconds", duration)
        audit = load_events(agent.task_state.task_id)
        task_record = TaskStore.load(agent.task_state.task_id)
        verification = run_verification(spec, workspace)

        payload = {
            "task_id": agent.task_state.task_id,
            "response": response,
            "trace": trace,
            "task_record": task_record,
            "verification": verification.__dict__ if verification else None,
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


def run_verification(spec: EvalTaskSpec, workspace: Path) -> VerificationResult | None:
    command = spec.outcome.verification_command
    if not command:
        return None
    proc = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        cwd=workspace,
    )
    output = proc.stdout
    if proc.stderr:
        output += f"\n[stderr]\n{proc.stderr}"
    return VerificationResult(exit_code=proc.returncode, output=output.strip())


def default_output_dir(base: Path) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return base / "runs" / stamp


def _approval_handler(spec: EvalTaskSpec):
    if spec.approval_mode == "reject_all":
        return lambda pending: False
    return lambda pending: True
