"""Tool registry."""

from .bash import BashTool
from .read import ReadFileTool
from .write import WriteFileTool
from .edit import EditFileTool
from .glob_tool import GlobTool
from .grep import GrepTool
from .agent import AgentTool
from .todo_write import TodoWriteTool

ALL_TOOLS = [
    BashTool(),
    ReadFileTool(),
    WriteFileTool(),
    EditFileTool(),
    GlobTool(),
    GrepTool(),
    TodoWriteTool(),
    AgentTool(),
]


def build_tool_registry(tools) -> dict[str, object]:
    """Build a name -> tool registry from a tool list."""
    return {t.name: t for t in tools}


def get_tool(name: str, registry: dict[str, object] | None = None):
    """Look up a tool by name."""
    if registry is not None:
        return registry.get(name)
    return build_tool_registry(ALL_TOOLS).get(name)
