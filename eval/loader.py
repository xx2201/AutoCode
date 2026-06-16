"""Task discovery and loading."""

from __future__ import annotations

from pathlib import Path

from .schema import EvalTaskSpec


def discover_tasks(tasks_dir: str | Path) -> list[Path]:
    base = Path(tasks_dir)
    files = list(base.glob("*.json"))
    files.extend(base.glob("*.yaml"))
    files.extend(base.glob("*.yml"))
    return sorted(files)


def load_tasks(tasks_dir: str | Path, task_ids: list[str] | None = None, tags: list[str] | None = None) -> list[EvalTaskSpec]:
    wanted = set(task_ids or [])
    required_tags = set(tags or [])
    tasks = []
    for path in discover_tasks(tasks_dir):
        spec = EvalTaskSpec.load(path)
        if wanted and spec.id not in wanted:
            continue
        if required_tags and not required_tags.intersection(spec.tags):
            continue
        tasks.append(spec)
    return tasks
