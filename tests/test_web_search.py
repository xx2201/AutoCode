from autocode.config import Config
from autocode.tools.factory import build_agent_tools
from autocode.tools.web_search import WebSearchTool


class _FakeTavilyClient:
    def __init__(self, response=None, error=None):
        self.response = response or {}
        self.error = error
        self.calls = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class _NoMcpTools:
    def initialize(self):
        return None

    def snapshot_tools(self):
        return []


def test_web_search_uses_bounded_basic_search_and_formats_sources():
    client = _FakeTavilyClient(
        {
            "results": [
                {
                    "title": "Python documentation",
                    "url": "https://docs.python.org/",
                    "content": "Official Python documentation.",
                    "score": 0.9321,
                }
            ],
            "usage": {"credits": 1},
        }
    )
    tool = WebSearchTool("test-secret", client=client)

    result = tool.execute(
        query="latest Python documentation",
        topic="general",
        time_range="month",
        max_results=3,
    )

    assert client.calls == [
        {
            "query": "latest Python documentation",
            "search_depth": "basic",
            "topic": "general",
            "max_results": 3,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
            "include_usage": True,
            "timeout": 20,
            "time_range": "month",
        }
    ]
    assert "untrusted web content" in result
    assert "https://docs.python.org/" in result
    assert "Relevance: 0.932" in result
    assert "Tavily credits used: 1" in result
    assert "test-secret" not in result


def test_web_search_redacts_key_from_provider_errors():
    tool = WebSearchTool(
        "test-secret",
        client=_FakeTavilyClient(error=RuntimeError("bad key test-secret")),
    )

    result = tool.execute(query="current information")

    assert result == "Error: Tavily web search failed: bad key [redacted]"


def test_web_search_validates_arguments_without_calling_provider():
    client = _FakeTavilyClient()
    tool = WebSearchTool("test-secret", client=client)

    assert tool.execute(query="").startswith("Error:")
    assert tool.execute(query="x", topic="invalid").startswith("Error:")
    assert tool.execute(query="x", max_results=11).startswith("Error:")
    assert client.calls == []


def test_factory_registers_web_search_only_when_tavily_is_configured():
    manager = _NoMcpTools()
    without_search = build_agent_tools(Config(), mcp_manager=manager)
    with_search = build_agent_tools(
        Config(tavily_api_key="test-secret"),
        mcp_manager=manager,
    )

    assert "web_search_local" not in {tool.name for tool in without_search}
    assert "web_search_local" in {tool.name for tool in with_search}
