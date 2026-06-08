"""AutoCode - Minimal AI coding agent inspired by Claude Code's architecture."""

__version__ = "0.3.0"

from autocode.agent import Agent
from autocode.llm import LLM
from autocode.config import Config
from autocode.tools import ALL_TOOLS

__all__ = ["Agent", "LLM", "Config", "ALL_TOOLS", "__version__"]

