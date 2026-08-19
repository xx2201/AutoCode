"""Named permission presets shared by agent and remote surfaces."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PermissionPreset:
    name: str
    sandbox_mode: str
    approval_policy: str


PERMISSION_PRESETS = {
    "workspace-write": PermissionPreset(
        name="workspace-write",
        sandbox_mode="workspace-write",
        approval_policy="ask",
    ),
    "danger-full-access": PermissionPreset(
        name="danger-full-access",
        sandbox_mode="danger-full-access",
        approval_policy="never",
    ),
}

DEFAULT_PERMISSION_PRESET = "workspace-write"


def resolve_permission_preset(name: str) -> PermissionPreset:
    try:
        return PERMISSION_PRESETS[name]
    except KeyError as exc:
        raise ValueError(f"Unsupported permission preset: {name}") from exc


def infer_permission_preset(sandbox_mode: str, approval_policy: str) -> str:
    for preset in PERMISSION_PRESETS.values():
        if (
            preset.sandbox_mode == sandbox_mode
            and preset.approval_policy == approval_policy
        ):
            return preset.name
    return "custom"
