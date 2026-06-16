"""Shared MCP runtime for stdio servers."""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .tools.base import Tool

MCP_PROTOCOL_VERSION = "2025-06-18"
_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_JSON_START_RE = re.compile(r"[{\\[]")
_DEFAULT_REQUEST_TIMEOUT = 20.0
_DEFAULT_INIT_TIMEOUT = max(
    15.0,
    float(os.getenv("AUTOCODE_MCP_INIT_TIMEOUT_MS", "120000")) / 1000.0,
)
_QUEUE_EOF = object()
_RUNTIME_REGISTRY: dict[tuple[str, str], "MCPManager"] = {}
_RUNTIME_LOCK = threading.Lock()


def _slug(value: str, fallback: str) -> str:
    cleaned = _NAME_RE.sub("_", value).strip("._-")
    return cleaned or fallback


def _coerce_schema(raw: Any) -> dict:
    if isinstance(raw, dict):
        schema = dict(raw)
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        schema.setdefault("required", [])
        return schema
    return {"type": "object", "properties": {}, "required": []}


def _summarize_content(block: dict) -> str:
    kind = block.get("type", "")
    if kind == "text":
        return str(block.get("text", ""))
    if kind == "image":
        return f"[image {block.get('mimeType', 'unknown')}]"
    if kind == "audio":
        return f"[audio {block.get('mimeType', 'unknown')}]"
    if kind == "resource_link":
        label = block.get("name") or block.get("uri") or "resource"
        return f"[resource_link] {label}"
    if kind == "resource":
        resource = block.get("resource", {})
        label = resource.get("uri") or resource.get("name") or "resource"
        return f"[resource] {label}"
    return json.dumps(block, ensure_ascii=False)


def _format_tool_result(result: dict) -> str:
    if result.get("resultType") == "input_required":
        return "Error: MCP tool requires additional interactive input, which AutoCode does not support yet."

    parts: list[str] = []
    for block in result.get("content", []) or []:
        if isinstance(block, dict):
            text = _summarize_content(block).strip()
            if text:
                parts.append(text)

    structured = result.get("structuredContent")
    if structured is not None:
        parts.append(json.dumps(structured, ensure_ascii=False, indent=2))

    text = "\n".join(part for part in parts if part).strip() or "(no content)"
    if result.get("isError"):
        return f"Error: {text}"
    return text


@dataclass(slots=True)
class MCPServerConfig:
    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    timeout: float = _DEFAULT_REQUEST_TIMEOUT
    disabled: bool = False


@dataclass(slots=True)
class MCPServerInfo:
    name: str
    status: str
    tool_count: int
    error: str = ""
    command: str = ""


class MCPServer:
    """One MCP stdio server process."""

    def __init__(self, config: MCPServerConfig, workspace_root: str):
        self.config = config
        self.workspace_root = Path(workspace_root).resolve()
        self.process: subprocess.Popen[str] | None = None
        self._stdout_queue: queue.Queue[object] = queue.Queue()
        self._stderr_tail: list[str] = []
        self._stderr_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._next_id = 0
        self._buffer = ""
        self._closed = False

    def start(self) -> None:
        if self.process is not None:
            return
        command = self.config.command
        if os.name == "nt" and Path(command).name.lower() == "npx":
            command = "npx.cmd"
        cwd = self.workspace_root if not self.config.cwd else self._resolve_cwd(self.config.cwd)
        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in self.config.env.items()})
        self.process = subprocess.Popen(
            [command, *self.config.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=0,
        )
        threading.Thread(target=self._pump_stdout, name=f"mcp-stdout-{self.config.name}", daemon=True).start()
        threading.Thread(target=self._pump_stderr, name=f"mcp-stderr-{self.config.name}", daemon=True).start()
        self._request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "AutoCode", "version": "0.3.0"},
            },
            timeout=_DEFAULT_INIT_TIMEOUT,
        )
        self._notify("notifications/initialized")

    def list_tools(self) -> list[dict]:
        result = self._request("tools/list", {})
        items = result.get("tools", [])
        return [item for item in items if isinstance(item, dict)]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict:
        return self._request("tools/call", {"name": name, "arguments": arguments}, timeout=self.config.timeout)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self.process
        self.process = None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=1.5)
            return
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=1.5)
            return
        except Exception:
            pass
        try:
            proc.kill()
            proc.wait(timeout=1.0)
        except Exception:
            pass

    def _resolve_cwd(self, raw_cwd: str) -> Path:
        path = Path(raw_cwd).expanduser()
        if not path.is_absolute():
            path = (self.workspace_root / path).resolve()
        else:
            path = path.resolve()
        return path

    def _pump_stdout(self) -> None:
        proc = self.process
        if proc is None or proc.stdout is None:
            return
        fd = proc.stdout.fileno()
        try:
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                self._push_messages(chunk.decode("utf-8", errors="replace"))
        finally:
            self._stdout_queue.put(_QUEUE_EOF)

    def _pump_stderr(self) -> None:
        proc = self.process
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            text = line.rstrip()
            if not text:
                continue
            with self._stderr_lock:
                self._stderr_tail.append(text)
                if len(self._stderr_tail) > 20:
                    self._stderr_tail = self._stderr_tail[-20:]

    def _push_messages(self, chunk: str) -> None:
        self._buffer += chunk
        while True:
            message = self._extract_content_length_message()
            if message is not None:
                self._stdout_queue.put(message)
                continue
            if self._buffer.startswith("Content-Length:"):
                return
            line = self._extract_json_line()
            if line is not None:
                self._stdout_queue.put(line)
                continue
            return

    def _extract_content_length_message(self) -> str | None:
        if not self._buffer.startswith("Content-Length:"):
            return None
        header_end = self._buffer.find("\r\n\r\n")
        delimiter_len = 4
        if header_end == -1:
            header_end = self._buffer.find("\n\n")
            delimiter_len = 2
        if header_end == -1:
            return None
        headers = self._buffer[:header_end].splitlines()
        length = 0
        for header in headers:
            if header.lower().startswith("content-length:"):
                length = int(header.split(":", 1)[1].strip())
                break
        body_start = header_end + delimiter_len
        if len(self._buffer) < body_start + length:
            return None
        body = self._buffer[body_start:body_start + length]
        self._buffer = self._buffer[body_start + length:]
        return body

    def _extract_json_line(self) -> str | None:
        match = _JSON_START_RE.search(self._buffer)
        if match is None:
            self._buffer = ""
            return None
        if match.start() > 0:
            self._buffer = self._buffer[match.start():]
        newline = self._buffer.find("\n")
        if newline == -1:
            return None
        line = self._buffer[:newline].strip()
        self._buffer = self._buffer[newline + 1:]
        return line or None

    def _notify(self, method: str, params: dict | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        if params:
            payload["params"] = params
        self._send(payload)

    def _request(self, method: str, params: dict | None = None, timeout: float = _DEFAULT_REQUEST_TIMEOUT) -> dict:
        if self.process is None or self.process.stdin is None:
            raise RuntimeError(f"MCP server '{self.config.name}' is not running")
        with self._request_lock:
            self._next_id += 1
            request_id = self._next_id
            payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
            if params is not None:
                payload["params"] = params
            self._send(payload)
            return self._await_response(request_id, timeout=timeout)

    def _send(self, payload: dict) -> None:
        if self.process is None or self.process.stdin is None:
            raise RuntimeError(f"MCP server '{self.config.name}' is not running")
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def _await_response(self, request_id: int, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"MCP request timed out after {timeout:.1f}s")
            try:
                item = self._stdout_queue.get(timeout=remaining)
            except queue.Empty as exc:
                raise RuntimeError(f"MCP request timed out after {timeout:.1f}s") from exc

            if item is _QUEUE_EOF:
                raise RuntimeError(self._server_closed_message())

            line = str(item).strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Invalid MCP JSON from '{self.config.name}': {line[:200]}") from exc

            if self._handle_server_message(message):
                continue

            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message["error"] or {}
                raise RuntimeError(str(error.get("message") or "unknown MCP error"))
            result = message.get("result")
            return result if isinstance(result, dict) else {}

    def _handle_server_message(self, message: dict) -> bool:
        method = message.get("method")
        if not isinstance(method, str):
            return False
        request_id = message.get("id")
        if request_id is None:
            return True
        if method == "ping":
            self._send({"jsonrpc": "2.0", "id": request_id, "result": {}})
            return True
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"Unsupported client method: {method}"},
            }
        )
        return True

    def _server_closed_message(self) -> str:
        with self._stderr_lock:
            stderr = "\n".join(self._stderr_tail)
        suffix = f"\n{stderr}" if stderr else ""
        return f"MCP server '{self.config.name}' exited unexpectedly.{suffix}"


class MCPManager:
    """Shared MCP manager with process-level server lifecycle."""

    def __init__(self, workspace_root: str, config_path: str | None = None):
        self.workspace_root = Path(workspace_root).resolve()
        self.config_path = self._resolve_config_path(config_path)
        self._servers: dict[str, MCPServer] = {}
        self._server_infos: dict[str, MCPServerInfo] = {}
        self._tools: list[MCPTool] = []
        self._lock = threading.RLock()
        self._init_started = False
        self._init_done = threading.Event()
        self._init_thread: threading.Thread | None = None
        self._closed = False

    def start_background(self) -> None:
        with self._lock:
            if self._init_started:
                return
            self._seed_server_infos()
            self._init_started = True
            self._init_thread = threading.Thread(target=self._initialize, name="autocode-mcp-init", daemon=True)
            self._init_thread.start()

    def initialize(self) -> None:
        with self._lock:
            if self._init_done.is_set():
                return
            if not self._init_started:
                self._seed_server_infos()
                self._init_started = True
                run_inline = True
            else:
                run_inline = False
        if run_inline:
            self._initialize()
            return
        self._init_done.wait()

    def wait_until_ready(self, timeout: float | None = None) -> bool:
        if not self._init_started:
            self.initialize()
            return True
        return self._init_done.wait(timeout)

    def snapshot_tools(self) -> list["MCPTool"]:
        with self._lock:
            return list(self._tools)

    def get_server_infos(self) -> list[MCPServerInfo]:
        with self._lock:
            infos = list(self._server_infos.values())
        return sorted(infos, key=lambda item: item.name)

    def call_tool(self, server_name: str, remote_name: str, arguments: dict[str, Any]) -> str:
        with self._lock:
            server = self._servers.get(server_name)
        if server is None:
            raise RuntimeError(f"MCP server '{server_name}' is not loaded")
        return _format_tool_result(server.call_tool(remote_name, arguments))

    def close(self) -> None:
        with self._lock:
            self._closed = True
            servers = list(self._servers.values())
            self._servers.clear()
            self._server_infos = {
                name: MCPServerInfo(
                    name=info.name,
                    status="stopped" if info.status in {"ready", "starting"} else info.status,
                    tool_count=info.tool_count,
                    error=info.error,
                    command=info.command,
                )
                for name, info in self._server_infos.items()
            }
        for server in servers:
            server.close()

    def _initialize(self) -> None:
        try:
            configs = self._read_config()
            tools: list[MCPTool] = []
            servers: dict[str, MCPServer] = {}
            infos: dict[str, MCPServerInfo] = {}
            name_counts: dict[str, int] = {}

            for config in configs:
                info = MCPServerInfo(
                    name=config.name,
                    status="disabled" if config.disabled else "starting",
                    tool_count=0,
                    command=config.command,
                )
                if config.disabled:
                    infos[config.name] = info
                    continue
                server = MCPServer(config, str(self.workspace_root))
                try:
                    server.start()
                    server_tools = server.list_tools()
                    servers[config.name] = server
                    info.status = "ready"
                    info.tool_count = len(server_tools)
                    for item in server_tools:
                        raw_name = f"mcp_{_slug(config.name, 'server')}_{_slug(str(item.get('name', 'tool')), 'tool')}"
                        index = name_counts.get(raw_name, 0)
                        name_counts[raw_name] = index + 1
                        public_name = raw_name if index == 0 else f"{raw_name}_{index + 1}"
                        tools.append(
                            MCPTool(
                                manager=self,
                                name=public_name,
                                server_name=config.name,
                                remote_name=str(item.get("name", "")),
                                description=f"[MCP:{config.name}] {item.get('description') or item.get('title') or item.get('name')}",
                                parameters=_coerce_schema(item.get("inputSchema")),
                            )
                        )
                except Exception as exc:
                    info.status = "error"
                    info.error = str(exc)
                    server.close()
                infos[config.name] = info

            with self._lock:
                self._servers = servers
                self._tools = tools
                self._server_infos = infos
        except Exception as exc:
            with self._lock:
                self._server_infos = {
                    "__config__": MCPServerInfo(
                        name="__config__",
                        status="error",
                        tool_count=0,
                        error=str(exc),
                        command=str(self.config_path),
                    )
                }
        finally:
            self._init_done.set()

    def _seed_server_infos(self) -> None:
        if self._server_infos:
            return
        if not self.config_path.exists():
            return
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            raw_servers = data.get("mcpServers", {})
            if not isinstance(raw_servers, dict):
                raise RuntimeError(f"Invalid MCP config in {self.config_path}: 'mcpServers' must be an object")
            for name, raw in raw_servers.items():
                if not isinstance(raw, dict):
                    continue
                self._server_infos[str(name)] = MCPServerInfo(
                    name=str(name),
                    status="disabled" if bool(raw.get("disabled", False)) else "starting",
                    tool_count=0,
                    command=str(raw.get("command", "")),
                )
        except Exception as exc:
            self._server_infos = {
                "__config__": MCPServerInfo(
                    name="__config__",
                    status="error",
                    tool_count=0,
                    error=str(exc),
                    command=str(self.config_path),
                )
            }

    def _resolve_config_path(self, raw_path: str | None) -> Path:
        if raw_path:
            path = Path(raw_path).expanduser()
            return path if path.is_absolute() else (self.workspace_root / path).resolve()
        return self.workspace_root / ".autocode" / "mcp.json"

    def _read_config(self) -> list[MCPServerConfig]:
        if not self.config_path.exists():
            return []
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw_servers = data.get("mcpServers", {})
        if not isinstance(raw_servers, dict):
            raise RuntimeError(f"Invalid MCP config in {self.config_path}: 'mcpServers' must be an object")
        configs: list[MCPServerConfig] = []
        for name, raw in raw_servers.items():
            if not isinstance(raw, dict):
                raise RuntimeError(f"Invalid MCP config for server '{name}': expected an object")
            command = str(raw.get("command", "")).strip()
            if not command:
                raise RuntimeError(f"Invalid MCP config for server '{name}': missing command")
            args = tuple(str(item) for item in raw.get("args", []) or [])
            env = {str(k): str(v) for k, v in (raw.get("env", {}) or {}).items()}
            timeout = float(raw.get("timeout", _DEFAULT_REQUEST_TIMEOUT))
            configs.append(
                MCPServerConfig(
                    name=str(name),
                    command=command,
                    args=args,
                    env=env,
                    cwd=raw.get("cwd"),
                    timeout=timeout,
                    disabled=bool(raw.get("disabled", False)),
                )
            )
        return configs


def get_shared_mcp_manager(workspace_root: str, config_path: str | None = None) -> MCPManager:
    workspace = str(Path(workspace_root).resolve())
    if config_path:
        resolved_path = Path(config_path).expanduser()
        config_key = str(resolved_path if resolved_path.is_absolute() else (Path(workspace) / resolved_path).resolve())
    else:
        config_key = str((Path(workspace) / ".autocode" / "mcp.json").resolve())
    key = (workspace, config_key)
    with _RUNTIME_LOCK:
        manager = _RUNTIME_REGISTRY.get(key)
        if manager is None or manager._closed:
            manager = MCPManager(workspace, config_key)
            _RUNTIME_REGISTRY[key] = manager
        return manager


class MCPTool(Tool):
    """Expose one remote MCP tool as a normal AutoCode tool."""

    def __init__(
        self,
        *,
        manager: MCPManager,
        name: str,
        server_name: str,
        remote_name: str,
        description: str,
        parameters: dict,
    ):
        self._manager = manager
        self.name = name
        self.server_name = server_name
        self.remote_name = remote_name
        self.description = description
        self.parameters = parameters

    def execute(self, **kwargs) -> str:
        try:
            return self._manager.call_tool(self.server_name, self.remote_name, kwargs)
        except Exception as exc:
            return f"Error: {exc}"

    def clone(self) -> "MCPTool":
        return type(self)(
            manager=self._manager,
            name=self.name,
            server_name=self.server_name,
            remote_name=self.remote_name,
            description=self.description,
            parameters=dict(self.parameters),
        )
