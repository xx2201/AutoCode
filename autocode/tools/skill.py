"""Progressively load reusable Agent Skills."""

from __future__ import annotations

from .base import ConcurrencySpec, Tool
from ..skills import SkillError


class SkillTool(Tool):
    name = "skill"
    description = (
        "Load a reusable SKILL.md workflow by name, or read one of its supporting files. "
        "Available skill names and descriptions are listed in the system prompt. "
        "Load a skill only when it matches the user's request."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Exact skill name from the available skills catalog",
            },
            "arguments": {
                "type": "string",
                "description": "Optional arguments substituted into $ARGUMENTS and $0..$9",
            },
            "resource": {
                "type": "string",
                "description": "Optional relative supporting-file path inside the skill directory",
            },
        },
        "required": ["name"],
    }

    def concurrency_spec(self, arguments: dict) -> ConcurrencySpec:
        return ConcurrencySpec.exclusive(
            "skill loading updates shared discovery and runtime context",
            main_thread=True,
        )

    def execute(self, name: str, arguments: str = "", resource: str = "") -> str:
        manager = getattr(self, "_skill_manager", None)
        if manager is None:
            return "Error: skill tool not initialized"
        try:
            if resource:
                return manager.read_resource(name, resource)
            skill = manager.discover().get(str(name or "").strip())
            if skill is not None and skill.disable_model_invocation:
                return f"Error: skill '{skill.name}' can only be invoked explicitly by the user"
            return manager.load(name, arguments)
        except (OSError, SkillError) as exc:
            return f"Error: {exc}"
