"""Tool registry."""

from .bash import BashTool
from .read import ReadTool
from .write import WriteFileTool
from .edit import EditFileTool
from .delete import DeletePathTool
from .glob_tool import GlobTool
from .grep import GrepTool
from .agent import AgentTool
from .todo_write import TodoWriteTool
from .process import StartProcessTool, ReadProcessOutputTool, WaitForProcessOutputTool, StopProcessTool
from .skill import SkillTool
from .web_search import WebSearchTool
from .web_fetch import WebFetchTool

ALL_TOOLS = [
    BashTool(),
    ReadTool(),
    WriteFileTool(),
    EditFileTool(),
    DeletePathTool(),
    GlobTool(),
    GrepTool(),
    StartProcessTool(),
    ReadProcessOutputTool(),
    WaitForProcessOutputTool(),
    StopProcessTool(),
    TodoWriteTool(),
    SkillTool(),
    AgentTool(),
    WebFetchTool(),
]


def build_tool_registry(tools) -> dict[str, object]:
    """Build a name -> tool registry from a tool list."""
    return {t.name: t for t in tools}


def get_tool(name: str, registry: dict[str, object] | None = None):
    """Look up a tool by name."""
    if registry is not None:
        return registry.get(name)
    return build_tool_registry(ALL_TOOLS).get(name)
