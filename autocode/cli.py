"""Interactive REPL - the user-facing terminal interface."""

import sys
import os
import argparse

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from prompt_toolkit.application import get_app
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.shortcuts import CompleteStyle

from .agent import Agent
from .config import Config
from .context import render_todos
from .llm import LLM, LiteLLM
from .mcp import get_shared_mcp_manager
from .tools.factory import build_agent_tools
from .state import (
    format_trace,
    list_sessions,
    load_checkpoint,
    load_trace,
)
from .workspaces import WorkspaceRegistry
from . import __version__

console = Console()
_PROMPT_MESSAGE = [("ansibrightblue bold", "You > ")]
_APPROVAL_PROMPT_MESSAGE = [("ansibrightyellow bold", "Approve > ")]
_APPROVAL_OPTIONS = [
    ("/approve", "Approve the pending tool call"),
    ("/approve_all", "Approve now and auto-approve later normal confirms"),
    ("/reject", "Reject the pending tool call"),
    ("/later", "Keep approval pending and return"),
]


class _ApprovalCompleter(Completer):
    def get_completions(self, document, complete_event):
        prefix = document.text_before_cursor.strip().lower()
        start_position = -len(document.text_before_cursor)
        for command, description in _APPROVAL_OPTIONS:
            if not prefix or command.startswith(prefix):
                yield Completion(
                    command,
                    start_position=start_position,
                    display=command,
                    display_meta=description,
                )


def _parse_args():
    p = argparse.ArgumentParser(
        prog="autocode",
        description="Minimal AI coding agent. Works with any OpenAI-compatible LLM.",
    )
    p.add_argument("-m", "--model", help="Model name (default: current configured model)")
    p.add_argument("--base-url", help="API base URL (default: current configured base URL)")
    p.add_argument("--api-key", help="API key (default: current configured API key)")
    p.add_argument("-p", "--prompt", help="One-shot prompt (non-interactive mode)")
    p.add_argument("-r", "--resume", metavar="ID", help="Resume a session")
    p.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args()


def main():
    args = _parse_args()
    config = Config.from_env()

    # CLI args override env vars
    if args.model:
        config.model = args.model
    if args.base_url:
        config.base_url = args.base_url
    if args.api_key:
        config.api_key = args.api_key

    if not config.model:
        console.print("[red bold]No model configured.[/]")
        console.print(
            "Set `AUTOCODE_MODEL`, pass `-m/--model`, or configure the current workspace `.env`.\n"
            f"Current workspace: [cyan]{config.workspace_root}[/cyan]"
        )
        sys.exit(1)

    if not config.api_key:
        console.print("[red bold]No API key found.[/]")
        console.print(
            "Set `AUTOCODE_API_KEY` for the agent runtime.\n"
            "\nExamples:\n"
            "  # OpenAI-compatible runtime\n"
            "  export AUTOCODE_API_KEY=sk-...\n"
            "  export AUTOCODE_BASE_URL=https://api.openai.com/v1\n"
            "  export AUTOCODE_MODEL=gpt-4o\n"
            "\n"
            "  # DeepSeek\n"
            "  export AUTOCODE_API_KEY=sk-...\n"
            "  export AUTOCODE_BASE_URL=https://api.deepseek.com\n"
            "  export AUTOCODE_MODEL=deepseek-chat\n"
            "\n"
            "  # Ollama (local)\n"
            "  export AUTOCODE_API_KEY=ollama\n"
            "  export AUTOCODE_BASE_URL=http://localhost:11434/v1\n"
            "  export AUTOCODE_MODEL=qwen2.5-coder\n"
        )
        sys.exit(1)

    # CLI 是 Workspace 的唯一注册入口；Web 只读取这份本机注册表。
    WorkspaceRegistry().register(config.workspace_root)

    llm_cls = LiteLLM if config.provider == "litellm" else LLM
    llm = llm_cls(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
    )
    mcp_manager = get_shared_mcp_manager(config.workspace_root, config.mcp_config_path)
    mcp_manager.start_background()
    agent = Agent(
        llm=llm,
        tools=build_agent_tools(config, mcp_manager=mcp_manager),
        tool_factory=lambda: build_agent_tools(config, mcp_manager=mcp_manager),
        mcp_manager=mcp_manager,
        own_mcp_manager=True,
        max_context_tokens=config.max_context_tokens,
        workspace_root=config.workspace_root,
        auto_approve=config.auto_approve,
    )

    if args.resume:
        loaded = _load_resumable_session(args.resume, config.workspace_root)
        if loaded:
            session_state, messages, loaded_model = loaded
            agent.restore_session(session_state, messages, loaded_model)
            if not args.model:
                agent.llm.model = loaded_model
                config.model = loaded_model
            current_task = session_state.current_task
            status = current_task.status if current_task else "idle"
            console.print(
                f"[green]Resumed session: {session_state.session_id} "
                f"(status: {status}, model: {agent.llm.model})[/green]"
            )
        else:
            console.print(f"[red]Session '{args.resume}' not found.[/red]")
            sys.exit(1)

    # one-shot mode
    if args.prompt:
        mcp_manager.wait_until_ready()
        agent._sync_mcp_tools()
        _run_once(agent, args.prompt)
        return

    # interactive REPL
    _repl(agent, config)


def _run_once(agent: Agent, prompt: str):
    """Non-interactive: run one prompt and exit."""
    streamed: list[str] = []

    def on_token(tok):
        streamed.append(tok)
        print(tok, end="", flush=True)

    def on_tool(name, kwargs):
        console.print(f"\n[dim]> {name}({_brief(kwargs)})[/dim]")

    response = agent.chat(prompt, on_token=on_token, on_tool=on_tool)
    if streamed:
        print()
    elif response:
        console.print(Markdown(response))


def _terminal_is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _write_terminal_sequence(sequence: str):
    sys.stdout.write(sequence)
    sys.stdout.flush()


def _welcome_panel(config: Config, current_model: str) -> Panel:
    left = (
        f"[bold]AutoCode[/bold] v{__version__}\n"
        f"Model: [cyan]{current_model}[/cyan]"
        + (f"\nBase: [dim]{config.base_url}[/dim]" if config.base_url else "")
        + "\n\n[bold bright_cyan] /\\_/\\\\[/]\n"
        + "[bold #ffd166]( o.o )[/]\n"
        + "[bold #ef476f] > ^ <[/]"
    )
    right = (
        "[bold]Workspace[/bold]\n"
        + f"[dim]{config.workspace_root}[/dim]\n\n"
        + "[bold]Tips[/bold]\n"
        + "Type [bold]/help[/bold] for commands.\n"
        + "Press [bold]Ctrl+C[/bold] to cancel, [bold]quit[/bold] to exit."
    )
    grid = Table.grid(expand=True, padding=(0, 3))
    grid.add_column(ratio=2)
    grid.add_column(ratio=3)
    grid.add_row(left, right)
    return Panel(grid, border_style="blue")


def _clear_terminal():
    if not _terminal_is_interactive():
        return
    if os.name == "nt":
        os.system("cls")
    _write_terminal_sequence("\x1b[3J\x1b[2J\x1b[H")


def _repl(agent: Agent, config: Config):
    """Interactive read-eval-print loop."""
    _clear_terminal()
    current_model = agent.llm.model
    console.print(_welcome_panel(config, current_model))
    hist_path = os.path.expanduser("~/.autocode_history")
    history = FileHistory(hist_path)

    # Enter submits, Escape+Enter inserts a newline (for pasting code blocks etc.)
    kb = KeyBindings()

    @kb.add("enter")
    def _submit(event):
        event.current_buffer.validate_and_handle()

    @kb.add("escape", "enter")
    def _newline(event):
        event.current_buffer.insert_text("\n")

    def _run_pending_approval(approved: bool, enable_auto_approve: bool = False):
        streamed: list[str] = []

        def on_token(tok):
            streamed.append(tok)
            print(tok, end="", flush=True)

        def on_tool(name, kwargs):
            console.print(f"\n[dim]> {name}({_brief(kwargs)})[/dim]")

        response = agent.approve_pending(
            approved=approved,
            on_token=on_token,
            on_tool=on_tool,
            approval_handler=None if config.auto_approve else _prompt_approval,
            enable_auto_approve=enable_auto_approve,
        )
        if streamed:
            print()
        else:
            console.print(Markdown(response))

    def _prompt_pending_approval() -> bool:
        handled = False
        while agent.task_state is not None and agent.task_state.pending_approval is not None:
            choice = _prompt_approval(agent.task_state.pending_approval)
            if choice is None:
                console.print(
                    "[yellow]Approval is still pending. "
                    "Choose Approve, Approve All, or Reject to continue.[/yellow]"
                )
                return handled
            _run_pending_approval(
                approved=choice in {"approve", "approve_all"},
                enable_auto_approve=choice == "approve_all",
            )
            handled = True
        return handled

    while True:
        if agent.task_state is not None and agent.task_state.pending_approval is not None:
            if _prompt_pending_approval():
                continue
        try:
            user_input = pt_prompt(
                _PROMPT_MESSAGE,
                history=history,
                multiline=True,
                key_bindings=kb,
                prompt_continuation="...  ",
            ).strip()
        except (EOFError, KeyboardInterrupt):
            agent.close()
            console.print("\nBye!")
            break

        if not user_input:
            continue

        if (
            agent.task_state is not None
            and agent.task_state.pending_approval is not None
            and not user_input.startswith("/")
        ):
            console.print(
                "[yellow]Approval is pending. Choose an action in the dialog or use "
                "/approve, /approve_all, or /reject.[/yellow]"
            )
            continue

        # built-in commands
        if user_input.lower() in ("quit", "exit", "/quit", "/exit"):
            agent.close()
            break
        if user_input == "/help":
            _show_help()
            continue
        if user_input == "/reset":
            agent.reset()
            console.print("[yellow]Conversation reset.[/yellow]")
            continue
        if user_input == "/tokens":
            p = agent.llm.total_prompt_tokens
            c = agent.llm.total_completion_tokens
            cache_read = getattr(agent.llm, "total_cache_read_tokens", 0)
            cache_miss = getattr(agent.llm, "total_cache_miss_tokens", 0)
            line = f"Tokens: [cyan]{p}[/cyan] prompt + [cyan]{c}[/cyan] completion = [bold]{p+c}[/bold] total"
            cost = agent.llm.estimated_cost
            if cost is not None:
                line += f"  (~${cost:.4f})"
            console.print(line)
            cache_total = cache_read + cache_miss
            cache_rate = f"{(cache_read / cache_total * 100):.1f}%" if cache_total else "n/a"
            console.print(
                "Prompt cache: "
                f"[cyan]{cache_read}[/cyan] hit + [cyan]{cache_miss}[/cyan] miss = [bold]{cache_total}[/bold] total  "
                f"(hit rate {cache_rate})"
            )
            continue
        if user_input == "/model" or user_input.startswith("/model "):
            new_model = user_input[7:].strip() if user_input.startswith("/model ") else ""
            if new_model:
                agent.llm.model = new_model
                config.model = new_model
                console.print(f"Switched to [cyan]{new_model}[/cyan]")
            else:
                console.print(f"Current model: [cyan]{config.model}[/cyan]")
            continue
        if user_input == "/compact":
            from .context import estimate_tokens
            before = estimate_tokens(agent.messages)
            compressed = agent.compact_context()
            after = estimate_tokens(agent.messages)
            if compressed.compressed:
                layers = ", ".join(compressed.layers)
                console.print(
                    f"[green]Compressed: {before} → {after} tokens ({len(agent.messages)} messages) "
                    f"[dim]layers={layers}[/dim][/green]"
                )
            else:
                console.print(f"[dim]Nothing to compress ({before} tokens, {len(agent.messages)} messages)[/dim]")
            continue
        if user_input == "/diff":
            from .tools.edit import _changed_files
            if not _changed_files:
                console.print("[dim]No files modified this session.[/dim]")
            else:
                console.print(f"[bold]Files modified this session ({len(_changed_files)}):[/bold]")
                for f in sorted(_changed_files):
                    console.print(f"  [cyan]{f}[/cyan]")
            continue
        if user_input == "/task":
            if agent.session_state is None:
                console.print("[dim]No active session.[/dim]")
            else:
                if agent.task_state is None:
                    console.print(f"Session: [cyan]{agent.session_state.session_id}[/cyan]  current task: [dim](none)[/dim]")
                    continue
                pending = ""
                if agent.task_state.pending_approval:
                    pending = f"  pending: {agent.task_state.pending_approval.tool_name}"
                auto = "on" if agent.task_state.auto_approve_for_task else "off"
                console.print(
                    f"Session: [cyan]{agent.session_state.session_id}[/cyan]  "
                    f"Task: [cyan]{agent.task_state.task_id}[/cyan]  "
                    f"title: [bold]{agent.task_state.title or '(untitled)'}[/bold]  "
                    f"status: [yellow]{agent.task_state.status}[/yellow]  "
                    f"steps: [bold]{agent.task_state.step_index}[/bold]  "
                    f"approve_all: [bold]{auto}[/bold]{pending}"
                )
            continue
        if user_input == "/todo":
            if agent.task_state is None:
                console.print("[dim]No active task.[/dim]")
            else:
                console.print(Panel(render_todos(agent.task_state.todos), title="Todo", border_style="dim"))
            continue
        if user_input == "/resume" or user_input.startswith("/resume "):
            target = user_input[8:].strip() if user_input.startswith("/resume ") else ""
            if target:
                loaded = _load_resumable_session(target, config.workspace_root)
                if loaded is None:
                    console.print(f"[red]Session '{target}' not found.[/red]")
                    continue
                session_state, messages, loaded_model = loaded
                agent.restore_session(session_state, messages, loaded_model)
                agent.llm.model = loaded_model
                config.model = loaded_model
                current_task = session_state.current_task
                status = current_task.status if current_task else "idle"
                console.print(
                    f"[green]Resumed session: {session_state.session_id} "
                    f"(status: {status}, model: {agent.llm.model})[/green]"
                )
                continue
            sessions = _resume_candidates(config.workspace_root, limit=10)
            if not sessions:
                console.print("[dim]No resumable sessions for the current project.[/dim]")
            else:
                for session in sessions:
                    console.print(
                        f"  [cyan]{session['session_id']}[/cyan] ({session['status']}, step {session['step_index']}, "
                        f"{session['model']}, {session['saved_at']})"
                    )
            continue
        if user_input == "/trace":
            if agent.session_state is None:
                console.print("[dim]No active session.[/dim]")
                continue
            trace = load_trace(agent.session_state.session_id)
            if trace is None:
                console.print("[dim]No trace recorded yet.[/dim]")
            else:
                console.print(Panel(format_trace(trace), title="Trace", border_style="dim"))
            continue
        if user_input == "/mcp":
            infos = agent.mcp_manager.get_server_infos() if agent.mcp_manager is not None else []
            if not infos:
                console.print("[dim]No MCP servers configured.[/dim]")
                continue
            table = Table(title="MCP Servers", show_header=True, header_style="bold")
            table.add_column("Server")
            table.add_column("Status")
            table.add_column("Tools", justify="right")
            table.add_column("Error")
            for info in infos:
                table.add_row(info.name, info.status, str(info.tool_count), info.error or "-")
            console.print(table)
            for info in infos:
                tool_names = [
                    tool.name for tool in agent.mcp_manager.snapshot_tools()
                    if getattr(tool, "server_name", "") == info.name
                ]
                if tool_names:
                    console.print(f"[bold]{info.name}[/bold]: " + ", ".join(tool_names))
            continue
        if user_input == "/approve":
            _run_pending_approval(True)
            continue
        if user_input == "/approve_all":
            _run_pending_approval(True, enable_auto_approve=True)
            continue
        if user_input == "/reject":
            _run_pending_approval(False)
            continue

        # call the agent
        streamed: list[str] = []

        def on_token(tok):
            streamed.append(tok)
            print(tok, end="", flush=True)

        def on_tool(name, kwargs):
            console.print(f"\n[dim]> {name}({_brief(kwargs)})[/dim]")

        try:
            response = agent.chat(
                user_input,
                on_token=on_token,
                on_tool=on_tool,
                approval_handler=None,
            )
            if agent.task_state is not None and agent.task_state.pending_approval is not None:
                if not _prompt_pending_approval():
                    console.print(Markdown(response))
                continue
            if streamed:
                print()  # newline after streamed tokens
            else:
                # response wasn't streamed (came after tool calls)
                console.print(Markdown(response))
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted.[/yellow]")
        except Exception as e:
            console.print(f"\n[red]Error: {e}[/red]")


def _show_help():
    console.print(Panel(
        "[bold]Commands:[/bold]\n"
        "  /help          Show this help\n"
        "  /reset         Clear conversation history\n"
        "  /model         Show current model\n"
        "  /model <name>  Switch model mid-conversation\n"
        "  /tokens        Show token usage\n"
        "  /compact       Compress conversation context\n"
        "  /diff          Show files modified this session\n"
        "  /resume        List resumable sessions for the current project\n"
        "  /resume <id>   Resume a session by id\n"
        "  /task          Show the current session and task state\n"
        "  /todo          Show the current todo list\n"
        "  /trace         Show the current session trace\n"
        "  /mcp           Show MCP server status and loaded tools\n"
        "  /approve       Approve the pending tool call\n"
        "  /approve_all   Approve this tool call and auto-approve later normal confirms\n"
        "  /reject        Reject the pending tool call\n"
        "  quit           Exit AutoCode\n"
        "\n"
        "[bold]Input:[/bold]\n"
        "  Enter          Submit message\n"
        "  Esc+Enter      Insert newline (for pasting code)",
        title="AutoCode Help",
        border_style="dim",
    ))


def _brief(kwargs: dict, maxlen: int = 80) -> str:
    s = ", ".join(f"{k}={repr(v)[:40]}" for k, v in kwargs.items())
    return s[:maxlen] + ("..." if len(s) > maxlen else "")


def _resume_candidates(workspace_root: str, limit: int = 10) -> list[dict]:
    return list_sessions(workspace_root=workspace_root, limit=limit)


def _load_resumable_session(session_id: str, workspace_root: str):
    allowed = {item["session_id"] for item in _resume_candidates(workspace_root, limit=200)}
    if session_id not in allowed:
        return None
    return load_checkpoint(session_id)


def _prompt_approval(pending) -> str | bool | None:
    command = ""
    if getattr(pending, "tool_name", "") == "bash":
        command = getattr(pending, "arguments", {}).get("command", "")

    body = (
        f"Tool: {pending.tool_name}\n"
        f"Reason: {pending.reason or 'confirmation required'}"
        + (f"\n\nCommand:\n{command}" if command else "")
    )
    console.print(Panel(body, title="Pending Approval", border_style="yellow"))

    completer = _ApprovalCompleter()
    while True:
        try:
            result = pt_prompt(
                _APPROVAL_PROMPT_MESSAGE,
                default="/",
                completer=completer,
                complete_style=CompleteStyle.MULTI_COLUMN,
                complete_while_typing=True,
                pre_run=lambda: get_app().current_buffer.start_completion(select_first=False),
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None

        if result in {"", "/later"}:
            return None
        if result == "/approve":
            return "approve"
        if result == "/approve_all":
            return "approve_all"
        if result == "/reject":
            return False
        console.print("[yellow]Choose /approve, /approve_all, /reject, or /later.[/yellow]")

