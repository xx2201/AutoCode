"""Base class for all tools."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolResult:
    """A tool result with optional multimodal content for the next model turn."""

    text: str
    model_content: list[dict] = field(default_factory=list)

    def __str__(self) -> str:
        return self.text


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

    def clone(self):
        """Build a fresh tool instance for a new agent."""
        return type(self)()

    def close(self) -> None:
        """Release tool-owned resources."""
        return None
