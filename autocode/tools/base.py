"""Base class for all tools."""

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


@dataclass(frozen=True)
class ToolResult:
    """A tool result with optional multimodal content for the next model turn."""

    text: str
    model_content: list[dict] = field(default_factory=list)
    is_error: bool = False

    def __str__(self) -> str:
        return self.text


class ConcurrencyMode(str, Enum):
    """How a tool call may share an execution group with sibling calls."""

    PARALLEL = "parallel"
    RESOURCE_SCOPED = "resource_scoped"
    EXCLUSIVE = "exclusive"


@dataclass(frozen=True)
class ConcurrencySpec:
    """Parameter-level concurrency declaration for one concrete tool call."""

    mode: ConcurrencyMode
    read_resources: frozenset[str] = field(default_factory=frozenset)
    write_resources: frozenset[str] = field(default_factory=frozenset)
    main_thread: bool = False
    reason: str = ""

    @classmethod
    def parallel(cls, reason: str = "") -> "ConcurrencySpec":
        return cls(mode=ConcurrencyMode.PARALLEL, reason=reason)

    @classmethod
    def resources(
        cls,
        *,
        reads: set[str] | frozenset[str] | None = None,
        writes: set[str] | frozenset[str] | None = None,
        reason: str = "",
    ) -> "ConcurrencySpec":
        return cls(
            mode=ConcurrencyMode.RESOURCE_SCOPED,
            read_resources=frozenset(reads or ()),
            write_resources=frozenset(writes or ()),
            reason=reason,
        )

    @classmethod
    def exclusive(cls, reason: str, *, main_thread: bool = False) -> "ConcurrencySpec":
        return cls(
            mode=ConcurrencyMode.EXCLUSIVE,
            main_thread=main_thread,
            reason=reason,
        )


class Tool(ABC):
    """Minimal tool interface. Subclass this to add new capabilities."""

    name: str
    description: str
    parameters: dict  # JSON Schema for the function args

    @abstractmethod
    def execute(self, **kwargs) -> str | ToolResult:
        """Run the tool and return text plus optional model-only content."""
        ...

    def schema(self) -> dict:
        """OpenAI function-calling schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def concurrency_spec(self, arguments: dict) -> ConcurrencySpec:
        """Return the execution constraints for one call.

        Unknown and third-party tools stay exclusive until they explicitly declare
        parameter-level safety.
        """
        return ConcurrencySpec.exclusive("tool does not declare concurrency safety")

    def file_resource(self, value: str) -> str:
        """Build a stable, case-normalized resource key for a workspace path."""
        fs = getattr(self, "_fs", None)
        path = fs.resolve_path(value) if fs is not None else Path(value).expanduser().resolve()
        normalized = os.path.normcase(os.path.normpath(str(path)))
        return f"file:{normalized}"

    def clone(self):
        """Build a fresh tool instance for a new agent."""
        return type(self)()

    def close(self) -> None:
        """Release tool-owned resources."""
        return None
