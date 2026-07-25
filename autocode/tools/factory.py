"""Tool construction helpers."""

from __future__ import annotations

from ..config import Config
from ..mcp import get_shared_mcp_manager
from . import ALL_TOOLS
from .web_search import WebSearchTool


def _clone_tool(tool):
    if hasattr(tool, "clone"):
        return tool.clone()
    return type(tool)()


def build_agent_tools(config: Config, extra_tools: list | None = None, mcp_manager=None) -> list:
    tools = [_clone_tool(tool) for tool in ALL_TOOLS]
    if config.tavily_api_key:
        tools.append(WebSearchTool(config.tavily_api_key))
    if extra_tools:
        tools.extend(_clone_tool(tool) for tool in extra_tools)
    manager = mcp_manager or get_shared_mcp_manager(config.workspace_root, config.mcp_config_path)
    if mcp_manager is None:
        manager.initialize()
    tools.extend(manager.snapshot_tools())
    return tools
