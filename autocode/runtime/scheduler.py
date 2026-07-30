"""Conflict-aware planning for a model-produced batch of tool calls."""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..tools.base import ConcurrencyMode, ConcurrencySpec


@dataclass(frozen=True)
class ExecutionGroup:
    """A set of calls that may execute concurrently."""

    group_id: int
    call_indexes: tuple[int, ...]
    mode: str


def plan_execution_groups(specs: list[ConcurrencySpec]) -> list[ExecutionGroup]:
    """Keep model order while grouping calls that do not conflict."""
    groups: list[ExecutionGroup] = []
    current_indexes: list[int] = []
    current_specs: list[ConcurrencySpec] = []

    def flush() -> None:
        if not current_indexes:
            return
        mode = (
            ConcurrencyMode.PARALLEL.value
            if len(current_indexes) > 1
            else current_specs[0].mode.value
        )
        groups.append(
            ExecutionGroup(
                group_id=len(groups) + 1,
                call_indexes=tuple(current_indexes),
                mode=mode,
            )
        )
        current_indexes.clear()
        current_specs.clear()

    for index, spec in enumerate(specs):
        if spec.mode == ConcurrencyMode.EXCLUSIVE:
            flush()
            current_indexes.append(index)
            current_specs.append(spec)
            flush()
            continue
        if any(_conflicts(spec, existing) for existing in current_specs):
            flush()
        current_indexes.append(index)
        current_specs.append(spec)
    flush()
    return groups


def _conflicts(left: ConcurrencySpec, right: ConcurrencySpec) -> bool:
    if left.mode == ConcurrencyMode.EXCLUSIVE or right.mode == ConcurrencyMode.EXCLUSIVE:
        return True
    return (
        _sets_overlap(left.write_resources, right.write_resources)
        or _sets_overlap(left.write_resources, right.read_resources)
        or _sets_overlap(left.read_resources, right.write_resources)
    )


def _sets_overlap(left: frozenset[str], right: frozenset[str]) -> bool:
    return any(_resources_overlap(a, b) for a in left for b in right)


def _resources_overlap(left: str, right: str) -> bool:
    if left == right:
        return True
    if not left.startswith("file:") or not right.startswith("file:"):
        return False
    left_path = os.path.normcase(os.path.normpath(left.removeprefix("file:")))
    right_path = os.path.normcase(os.path.normpath(right.removeprefix("file:")))
    try:
        common = os.path.commonpath((left_path, right_path))
    except ValueError:
        return False
    return common == left_path or common == right_path
