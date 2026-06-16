# Agent Eval System

This directory contains an independent evaluation harness for `AutoCode`.

It is designed around the evaluation ideas emphasized by major agent stacks:

- final outcome evaluation
- trajectory evaluation
- safety evaluation
- recovery evaluation
- efficiency evaluation

The harness is intentionally local-first. It does not depend on LangSmith, OpenAI Evals,
or any hosted evaluation product.

It supports both deterministic rule grading and an optional LLM judge.

## Structure

```text
eval/
├── fixtures/      sample workspaces copied per trial
├── tasks/         task JSON definitions
├── graders.py     outcome / trajectory / safety / recovery / efficiency graders
├── harness.py     isolated workspace execution
├── judge.py       optional LLM judge
├── loader.py      task discovery
├── report.py      summary.json + report.md generation
├── runner.py      CLI entrypoint
└── schema.py      task schema
```

## Usage

List tasks:

```powershell
python -m eval.runner --list
```

Run all tasks:

```powershell
python -m eval.runner
```

If `--model` is omitted, eval runs default to `MiniMax-M2.7`. This only affects `eval/` and does not change the main CLI or remote runtimes.

Run a subset:

```powershell
python -m eval.runner --task debug-billing-settlement-03
```

Override trials or model:

```powershell
python -m eval.runner --trials 3 --model MiniMax-M2.7
```

Run with an LLM judge:

```powershell
python -m eval.runner --judge-model qwen-max
```

Disable the judge:

```powershell
python -m eval.runner --disable-llm-judge
```

Outputs are written under:

```text
eval/runs/<timestamp>/
```

Each trial gets:

- a copied workspace fixture
- task artifacts from the agent runtime
- `trial.json`

The run root gets:

- `summary.json`
- `report.md`

## Task Schema

Each task JSON contains:

- `id`
- `title`
- `prompt`
- `fixture`
- `tags`
- `trials`
- `approval_mode`
- `expectations`

Expectation groups:

- `outcome`
- `trajectory`
- `safety`
- `recovery`
- `efficiency`
- `judge`

## Notes

- The harness uses the same model configuration as `AutoCode`, loaded from environment variables.
- The optional LLM judge reads `AUTOCODE_EVAL_*` or `DASHSCOPE_*` environment variables.
- It is independent from the main app runtime, but reuses the `Agent` implementation and task artifacts (`trace.json`, `audit.jsonl`, `task.json`).
- The current bundled task is intentionally large and noisy. It is meant to stress long-context debugging, scoped edits, and verification discipline.

## Task Families

The bundled benchmark currently focuses on one high-difficulty family:

- long-context multi-file regression debugging with misleading docs and many distractor files

