"""Agent Skills discovery and progressive content loading."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

import yaml

_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_MAX_DESCRIPTION_CHARS = 1536
_MAX_SKILL_CHARS = 50_000
_MAX_RESOURCE_CHARS = 100_000


class SkillError(ValueError):
    """Raised when a skill cannot be discovered or loaded safely."""


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    description: str
    path: Path
    source: str
    disable_model_invocation: bool = False
    user_invocable: bool = True

    @property
    def directory(self) -> Path:
        return self.path.parent


class SkillManager:
    """Discover Agent Skills while loading full instructions only on demand."""

    def __init__(self, workspace_root: str, home: str | Path | None = None):
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.home = Path(home).expanduser().resolve() if home is not None else Path.home().resolve()

    def discover(self) -> dict[str, SkillDefinition]:
        """Return effective user skills by name, with AutoCoder scope taking priority."""
        skills: dict[str, SkillDefinition] = {}
        for source, root in self._skill_roots():
            if not root.is_dir():
                continue
            for skill_file in sorted(root.glob("*/SKILL.md")):
                try:
                    definition = self._read_definition(skill_file, source)
                except (OSError, SkillError, yaml.YAMLError):
                    continue
                skills[definition.name] = definition
        return skills

    def catalog_block(self) -> str:
        """Render model-invocable metadata without loading any SKILL.md body."""
        visible = [
            skill
            for skill in self.discover().values()
            if not skill.disable_model_invocation
        ]
        if not visible:
            return ""
        lines = [
            "Available reusable workflows are listed below as metadata only.",
            "Call the `skill` tool when one matches the current request; the full instructions load only then.",
        ]
        lines.extend(
            f"- **{skill.name}**: {skill.description}"
            for skill in sorted(visible, key=lambda item: item.name)
        )
        return "\n".join(lines)

    def load(self, name: str, arguments: str = "") -> str:
        skill = self._require_skill(name)
        _, body = self._parse_skill_file(skill.path)
        rendered = self._substitute_arguments(body, arguments, skill.directory)
        if len(rendered) > _MAX_SKILL_CHARS:
            rendered = rendered[:_MAX_SKILL_CHARS] + "\n\n... (skill content truncated)"
        return (
            f"[Loaded skill: {skill.name}]\n"
            f"Source: {skill.source}\n"
            f"Skill directory: {skill.directory}\n\n"
            f"{rendered.strip()}"
        )

    def read_resource(self, name: str, resource: str) -> str:
        skill = self._require_skill(name)
        relative = Path(str(resource or ""))
        if not resource or relative.is_absolute():
            raise SkillError("resource must be a relative path inside the skill directory")
        target = (skill.directory / relative).resolve()
        try:
            target.relative_to(skill.directory.resolve())
        except ValueError as exc:
            raise SkillError("resource must stay inside the skill directory") from exc
        if not target.is_file():
            raise SkillError(f"skill resource not found: {resource}")
        text = target.read_text(encoding="utf-8", errors="replace")
        if len(text) > _MAX_RESOURCE_CHARS:
            text = text[:_MAX_RESOURCE_CHARS] + "\n\n... (skill resource truncated)"
        return f"[Skill resource: {skill.name}/{relative.as_posix()}]\n\n{text}"

    def explicit_invocation(self, user_input: str) -> str | None:
        """Resolve `/skill-name args` when the named skill is user-invocable."""
        first_line = str(user_input or "").splitlines()[0].strip()
        if not first_line.startswith("/") or len(first_line) == 1:
            return None
        command, _, arguments = first_line[1:].partition(" ")
        skill = self.discover().get(command)
        if skill is None:
            return None
        if not skill.user_invocable:
            raise SkillError(f"skill '{skill.name}' is not user-invocable")
        return self.load(skill.name, arguments.strip())

    def _require_skill(self, name: str) -> SkillDefinition:
        normalized = str(name or "").strip()
        skills = self.discover()
        skill = skills.get(normalized)
        if skill is None:
            available = ", ".join(sorted(skills)) or "(none)"
            raise SkillError(f"unknown skill '{normalized}'. Available skills: {available}")
        return skill

    def _skill_roots(self) -> list[tuple[str, Path]]:
        # Later roots override earlier roots. AutoCoder's own namespace wins when
        # the same skill also exists in the cross-agent `.agents` directory.
        return [
            ("user:.agents", self.home / ".agents" / "skills"),
            ("user:.autocode", self.home / ".autocode" / "skills"),
        ]

    def _read_definition(self, path: Path, source: str) -> SkillDefinition:
        metadata, body = self._parse_skill_file(path)
        name = str(metadata.get("name") or path.parent.name).strip()
        if not _SKILL_NAME_RE.fullmatch(name):
            raise SkillError(f"invalid skill name: {name}")
        description = str(metadata.get("description") or self._first_paragraph(body) or name).strip()
        when_to_use = str(metadata.get("when_to_use") or "").strip()
        if when_to_use:
            description = f"{description} {when_to_use}".strip()
        description = re.sub(r"\s+", " ", description)[:_MAX_DESCRIPTION_CHARS]
        return SkillDefinition(
            name=name,
            description=description,
            path=path.resolve(),
            source=source,
            disable_model_invocation=bool(metadata.get("disable-model-invocation", False)),
            user_invocable=bool(metadata.get("user-invocable", True)),
        )

    @staticmethod
    def _parse_skill_file(path: Path) -> tuple[dict, str]:
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            return {}, text
        try:
            end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
        except StopIteration as exc:
            raise SkillError(f"unterminated YAML frontmatter: {path}") from exc
        metadata = yaml.safe_load("\n".join(lines[1:end])) or {}
        if not isinstance(metadata, dict):
            raise SkillError(f"skill frontmatter must be an object: {path}")
        return metadata, "\n".join(lines[end + 1 :])

    @staticmethod
    def _first_paragraph(body: str) -> str:
        for paragraph in re.split(r"\n\s*\n", body.strip()):
            cleaned = re.sub(r"^#+\s*", "", paragraph.strip())
            if cleaned:
                return cleaned
        return ""

    @staticmethod
    def _substitute_arguments(body: str, arguments: str, skill_directory: Path) -> str:
        raw = str(arguments or "")
        try:
            positional = shlex.split(raw, posix=True)
        except ValueError:
            positional = raw.split()
        rendered = body.replace("$ARGUMENTS", raw)
        rendered = rendered.replace("${AUTOCODE_SKILL_DIR}", str(skill_directory))
        for index in range(9, -1, -1):
            value = positional[index] if index < len(positional) else ""
            rendered = re.sub(rf"\${index}(?!\d)", lambda _match, item=value: item, rendered)
        return rendered
