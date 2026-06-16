import json
import sys
import textwrap

from autocode.config import Config
from autocode.mcp import get_shared_mcp_manager
from autocode.tools.factory import build_agent_tools


def _write_fake_mcp_server(path, *, content_length: bool = False):
    template = """
            import json
            import sys

            def reply(msg):
                payload = json.dumps(msg)
                if __CONTENT_LENGTH__:
                    sys.stdout.buffer.write(f"Content-Length: {len(payload)}\\r\\n\\r\\n{payload}".encode("utf-8"))
                    sys.stdout.buffer.flush()
                else:
                    sys.stdout.write(payload + "\\n")
                    sys.stdout.flush()

            for raw in sys.stdin:
                raw = raw.strip()
                if not raw:
                    continue
                msg = json.loads(raw)
                method = msg.get("method")
                if method == "initialize":
                    reply({
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "result": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "fake", "version": "1.0.0"},
                        },
                    })
                elif method == "notifications/initialized":
                    continue
                elif method == "tools/list":
                    reply({
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "result": {
                            "tools": [
                                {
                                    "name": "echo",
                                    "description": "Echo a message",
                                    "inputSchema": {
                                        "type": "object",
                                        "properties": {
                                            "message": {"type": "string"},
                                        },
                                        "required": ["message"],
                                    },
                                }
                            ]
                        },
                    })
                elif method == "tools/call":
                    message = msg.get("params", {}).get("arguments", {}).get("message", "")
                    reply({
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "result": {
                            "content": [{"type": "text", "text": f"echo:{message}"}],
                            "isError": False,
                        },
                    })
                else:
                    reply({
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "error": {"code": -32601, "message": f"unknown method: {method}"},
                    })
            """
    path.write_text(
        textwrap.dedent(
            template.replace("__CONTENT_LENGTH__", repr(content_length))
        ),
        encoding="utf-8",
    )


def test_build_agent_tools_loads_mcp_tool(tmp_path):
    server = tmp_path / "fake_mcp_server.py"
    _write_fake_mcp_server(server)
    config_dir = tmp_path / ".autocode"
    config_dir.mkdir()
    (config_dir / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fake": {
                        "command": sys.executable,
                        "args": [str(server)],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    config = Config(model="demo", api_key="secret", workspace_root=str(tmp_path))
    manager = get_shared_mcp_manager(config.workspace_root, config.mcp_config_path)

    tools = build_agent_tools(config)
    infos = manager.get_server_infos()

    mcp_tool = next(tool for tool in tools if tool.name == "mcp_fake_echo")
    assert mcp_tool.execute(message="hello") == "echo:hello"
    assert infos[0].status == "ready"
    assert infos[0].tool_count == 1
    assert get_shared_mcp_manager(config.workspace_root, config.mcp_config_path) is manager
    manager.close()
    assert mcp_tool.execute(message="again").startswith("Error:")


def test_mcp_manager_parses_content_length_messages(tmp_path):
    server = tmp_path / "fake_mcp_server_content_length.py"
    _write_fake_mcp_server(server, content_length=True)
    config_dir = tmp_path / ".autocode"
    config_dir.mkdir()
    (config_dir / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fake": {
                        "command": sys.executable,
                        "args": [str(server)],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    config = Config(model="demo", api_key="secret", workspace_root=str(tmp_path))

    tools = build_agent_tools(config)

    mcp_tool = next(tool for tool in tools if tool.name == "mcp_fake_echo")
    assert mcp_tool.execute(message="framed") == "echo:framed"
