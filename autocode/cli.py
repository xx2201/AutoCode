"""Interactive REPL - the user-facing terminal interface."""

import sys
import os
import argparse
import threading

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from prompt_toolkit.application import get_app
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import CompleteStyle

from .agent import Agent
from .config import Config
from .context import render_todos
from .llm import llm_class_for_provider
from .message_content import content_text, is_internal_visual_context
from .mcp import get_shared_mcp_manager
from .tools.factory import build_agent_tools
from .state import (
    format_trace,
    list_sessions,
    load_checkpoint,
    load_trace,
    save_turn_queue,
)
from .state.changes import ChangeSetStore
from .workspaces import WorkspaceRegistry
from . import __version__

console = Console()
_PROMPT_MESSAGE = [("ansibrightblue bold", "You > ")]
_APPROVAL_PROMPT_MESSAGE = [("ansibrightyellow bold", "Approve > ")]
_APPROVAL_OPTIONS = [
    ("/approve", "Approve the pending tool call"),
    ("/approve_scope", "Approve and allow the displayed scope for this task"),
    ("/reject", "Reject the pending tool call"),
    ("/later", "Keep approval pending and return"),
]


def _configure_utf8_stdio() -> None:
    """Keep redirected Windows CLI output from falling back to a legacy code page."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


class _AgentWorker:
    """Run Agent turns serially while the terminal remains interactive."""

    def __init__(self, agent: Agent):
        self.agent = agent
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._running = False
        self._pending_changes = None

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def start_chat(self, prompt: str) -> bool:
        return self._start("chat", prompt=prompt)

    def start_edit(self, turn_id: str, prompt: str) -> bool:
        return self._start("edit", turn_id=turn_id, prompt=prompt)

    def start_approval(self, approved: bool, *, grant_scope: bool = False) -> bool:
        return self._start(
            "approval",
            approved=approved,
            grant_scope=grant_scope,
        )

    def deliver(self, content: str, mode: str) -> str:
        """Deliver input atomically with worker completion/queue draining."""
        with self._lock:
            if not self._running:
                return "idle"
            active_turn_id = self.agent.turn_controller.active_turn_id
            if mode == "steer" and active_turn_id:
                self.agent.turn_controller.steer(
                    content,
                    expected_turn_id=active_turn_id,
                )
                return "steer"
            self.agent.turn_controller.queue(
                content,
                expected_turn_id=active_turn_id,
            )
            self._persist_queue()
            return "queue"

    def wait(self, timeout: float | None = None) -> None:
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _start(self, action: str, **kwargs) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._thread = threading.Thread(
                target=self._run,
                args=(action, kwargs),
                name="autocode-cli-agent",
                daemon=True,
            )
            self._thread.start()
            return True

    def _run(self, action: str, kwargs: dict) -> None:
        try:
            self._run_action(action, **kwargs)
            while True:
                with self._lock:
                    if self._has_pending_approval():
                        self._running = False
                        return
                    queued = self.agent.turn_controller.pop_queued()
                    self._persist_queue()
                    if queued is None:
                        self._running = False
                        return
                console.print("\n[dim]Starting queued follow-up.[/dim]")
                self._run_action("chat", prompt=queued.content)
        except Exception as exc:
            console.print(f"\n[red]Error: {exc}[/red]")
            with self._lock:
                self._running = False

    def _run_action(self, action: str, **kwargs) -> None:
        streamed: list[str] = []
        change_store = None
        change_before = None
        change_turn_id = ""
        if action == "approval" and self._pending_changes is not None:
            change_store, change_before, change_turn_id = self._pending_changes

        def on_token(token):
            streamed.append(token)
            print(token, end="", flush=True)

        def on_tool(name, arguments):
            console.print(f"\n[dim]> {name}({_brief(arguments)})[/dim]")

        common = {
            "on_token": on_token,
            "on_tool": on_tool,
            "approval_handler": None,
        }

        def capture_turn(event: str, data: dict) -> None:
            nonlocal change_store, change_before, change_turn_id
            if event != "turn_started":
                return
            change_turn_id = str(data.get("task_id", ""))
            try:
                change_store = ChangeSetStore(
                    self.agent.workspace_root,
                    str(data.get("session_id", "")),
                )
                change_before = change_store.capture_before(change_turn_id)
                self._pending_changes = (change_store, change_before, change_turn_id)
            except (ValueError, RuntimeError) as exc:
                change_store = None
                change_before = None
                console.print(f"[yellow]Per-turn Undo unavailable: {exc}[/yellow]")

        hooks = getattr(self.agent, "hooks", None)
        if hooks is not None and action in {"chat", "edit"}:
            hooks.on("turn_started", capture_turn)
        try:
            if action == "chat":
                response = self.agent.chat(kwargs["prompt"], **common)
            elif action == "edit":
                response = self.agent.edit_last_turn(
                    kwargs["turn_id"],
                    kwargs["prompt"],
                    **common,
                )
            elif action == "approval":
                response = self.agent.approve_pending(
                    approved=kwargs["approved"],
                    grant_scope=kwargs.get("grant_scope", False),
                    **common,
                )
            else:
                raise ValueError(f"Unknown worker action: {action}")
        finally:
            if hooks is not None and action in {"chat", "edit"}:
                hooks.off("turn_started", capture_turn)
            if (
                change_store is not None
                and change_before is not None
                and not self._has_pending_approval()
            ):
                change_store.capture_after(change_turn_id, change_before)
                self._pending_changes = None

        if streamed:
            print()
        elif response:
            console.print(Markdown(response))

    def _persist_queue(self) -> None:
        session = self.agent.session_state
        if session is None:
            return
        queued = [item.to_dict() for item in self.agent.turn_controller.queued()]
        save_turn_queue(session.session_id, queued)

    def _has_pending_approval(self) -> bool:
        task = self.agent.task_state
        return task is not None and getattr(task, "pending_tool_batch", None) is not None


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
        description="Local coding agent with Anthropic Messages and Chat Completions support.",
    )
    p.add_argument("-m", "--model", help="Model name (default: current configured model)")
    p.add_argument("--base-url", help="API base URL (default: current configured base URL)")
    p.add_argument("--api-key", help="API key (default: current configured API key)")
    p.add_argument(
        "--provider",
        choices=("anthropic", "openai", "litellm"),
        help="API provider/protocol (default: anthropic Messages)",
    )
    p.add_argument("-p", "--prompt", help="One-shot prompt (non-interactive mode)")
    p.add_argument("-r", "--resume", metavar="ID", help="Resume a session")
    p.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args()


def main():
    _configure_utf8_stdio()
    args = _parse_args()
    config = Config.from_env()

    # CLI args override env vars
    if args.model:
        config.model = args.model
    if args.base_url:
        config.base_url = args.base_url
    if args.api_key:
        config.api_key = args.api_key
    if args.provider:
        config.provider = args.provider

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
            "  # Anthropic Messages (default)\n"
            "  export AUTOCODE_API_KEY=sk-...\n"
            "  export AUTOCODE_BASE_URL=https://api.anthropic.com\n"
            "  export AUTOCODE_MODEL=claude-sonnet-4-6\n"
            "  export AUTOCODE_PROVIDER=anthropic\n"
            "\n"
            "  # OpenAI-compatible Chat Completions\n"
            "  export AUTOCODE_API_KEY=sk-...\n"
            "  export AUTOCODE_BASE_URL=https://api.deepseek.com\n"
            "  export AUTOCODE_MODEL=deepseek-chat\n"
            "  export AUTOCODE_PROVIDER=openai\n"
            "\n"
            "  # Ollama (local)\n"
            "  export AUTOCODE_API_KEY=ollama\n"
            "  export AUTOCODE_BASE_URL=http://localhost:11434/v1\n"
            "  export AUTOCODE_MODEL=qwen2.5-coder\n"
            "  export AUTOCODE_PROVIDER=openai\n"
        )
        sys.exit(1)

    # CLI 是 Workspace 的唯一注册入口；Web 只读取这份本机注册表。
    WorkspaceRegistry().register(config.workspace_root)

    llm_cls = llm_class_for_provider(config.provider)
    llm = llm_cls(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        langfuse_public_key=config.langfuse_public_key,
        langfuse_secret_key=config.langfuse_secret_key,
        langfuse_base_url=config.langfuse_base_url,
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
        max_output_tokens=config.max_tokens,
        workspace_root=config.workspace_root,
        permission_mode=config.permission_mode,
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
            _render_conversation_history(messages)
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
        + "Press [bold]Ctrl+C[/bold] to clear input, [bold]quit[/bold] to exit."
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
    worker = _AgentWorker(agent)
    submit_mode = {"value": "steer"}

    # Enter submits, Escape+Enter inserts a newline (for pasting code blocks etc.)
    kb = KeyBindings()

    @kb.add("enter")
    def _submit(event):
        submit_mode["value"] = "steer"
        event.current_buffer.validate_and_handle()

    @kb.add("tab")
    def _queue(event):
        submit_mode["value"] = "queue"
        event.current_buffer.validate_and_handle()

    @kb.add("escape", "enter")
    def _newline(event):
        event.current_buffer.insert_text("\n")

    def _prompt_pending_approval() -> bool:
        batch = agent.task_state.pending_tool_batch
        pending_items = batch.unresolved() if batch else []
        if not pending_items:
            return False
        choice = _prompt_approval(pending_items[0])
        if choice is None:
            console.print(
                "[yellow]Approval is still pending. "
                "Choose Approve, Approve All, or Reject to continue.[/yellow]"
            )
            return False
        return worker.start_approval(
            approved=choice in {"approve", "approve_scope"},
            grant_scope=choice == "approve_scope",
        )

    with patch_stdout(raw=True):
        while True:
            if (
                not worker.is_running
                and agent.task_state is not None
                and agent.task_state.pending_tool_batch is not None
            ):
                if _prompt_pending_approval():
                    continue
            try:
                user_input = pt_prompt(
                    _PROMPT_MESSAGE,
                    history=history,
                    multiline=True,
                    key_bindings=kb,
                    prompt_continuation="...  ",
                    bottom_toolbar=lambda: _context_toolbar(agent, worker.is_running),
                ).strip()
            except EOFError:
                if worker.is_running:
                    console.print("\n[yellow]Agent is still running; wait for it before exiting.[/yellow]")
                    continue
                agent.close()
                console.print("\nBye!")
                break
            except KeyboardInterrupt:
                if worker.is_running:
                    console.print("\n[yellow]Input cleared; the active Agent turn is still running.[/yellow]")
                    continue
                agent.close()
                console.print("\nBye!")
                break

            if not user_input:
                continue

            if (
                agent.task_state is not None
                and agent.task_state.pending_tool_batch is not None
                and not worker.is_running
                and not user_input.startswith("/")
            ):
                console.print(
                    "[yellow]Approval is pending. Choose an action in the dialog or use "
                    "/approve, /approve_scope, or /reject.[/yellow]"
                )
                continue

            if worker.is_running and not user_input.startswith("/"):
                delivered = worker.deliver(user_input, submit_mode["value"])
                if delivered == "steer":
                    console.print("[dim]Guidance sent to the active turn.[/dim]")
                elif delivered == "queue":
                    console.print("[dim]Queued for the next turn.[/dim]")
                else:
                    worker.start_chat(user_input)
                continue

            # built-in commands
            if user_input.lower() in ("quit", "exit", "/quit", "/exit"):
                if worker.is_running:
                    console.print("[yellow]Agent is still running; wait for it before exiting.[/yellow]")
                    continue
                agent.close()
                break
            if user_input == "/help":
                _show_help()
                continue
            if worker.is_running:
                console.print(
                    "[yellow]This command waits for the active turn. "
                    "Use Enter to steer or Tab to queue a follow-up.[/yellow]"
                )
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
                usage = agent.context_usage()
                console.print(
                    "Context window: "
                    f"[bold]{usage['used_percent']:.1f}% used[/bold] "
                    f"([cyan]{_format_token_count(usage['used_tokens'])}[/cyan] / "
                    f"{_format_token_count(usage['window_tokens'])} tokens, "
                    f"{_format_token_count(usage['remaining_tokens'])} left)"
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
                    batch = agent.task_state.pending_tool_batch
                    if batch:
                        unresolved = batch.unresolved()
                        pending = f"  pending approvals: {len(unresolved)}"
                    console.print(
                        f"Session: [cyan]{agent.session_state.session_id}[/cyan]  "
                        f"Task: [cyan]{agent.task_state.task_id}[/cyan]  "
                        f"title: [bold]{agent.task_state.title or '(untitled)'}[/bold]  "
                        f"status: [yellow]{agent.task_state.status}[/yellow]  "
                        f"steps: [bold]{agent.task_state.step_index}[/bold]  "
                        f"permissions: [bold]{agent.policy.permission_mode}[/bold]{pending}"
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
                    _render_conversation_history(messages)
                    continue
                sessions = _resume_candidates(config.workspace_root, limit=10)
                if not sessions:
                    console.print("[dim]No resumable sessions for the current project.[/dim]")
                else:
                    for session in sessions:
                        title = session["title"] or session["session_id"]
                        console.print(
                            f"  [bold]{title}[/bold]\n"
                            f"    [cyan]{session['session_id']}[/cyan] ({session['status']}, step {session['step_index']}, "
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
                worker.start_approval(True)
                continue
            if user_input == "/approve_scope":
                worker.start_approval(True, grant_scope=True)
                continue
            if user_input == "/permissions" or user_input.startswith("/permissions "):
                requested = user_input[len("/permissions"):].strip()
                if requested not in {"ask", "full_access"}:
                    console.print("[yellow]Usage: /permissions ask|full_access[/yellow]")
                    continue
                agent.set_permission_mode(requested)
                config.permission_mode = requested
                console.print(f"[green]Permission mode: {requested}[/green]")
                continue
            if user_input == "/reject":
                worker.start_approval(False)
                continue
            if user_input == "/edit-last" or user_input.startswith("/edit-last "):
                try:
                    prompt = user_input[len("/edit-last"):].strip()
                    turn_id, previous_prompt = _last_editable_prompt(agent)
                    if not prompt:
                        prompt = pt_prompt(
                            [("ansibrightblue bold", "Edit last > ")],
                            default=previous_prompt,
                            multiline=True,
                            key_bindings=kb,
                            prompt_continuation="...  ",
                        ).strip()
                    if prompt:
                        worker.start_edit(turn_id, prompt)
                except ValueError as exc:
                    console.print(f"[yellow]{exc}[/yellow]")
                continue
            if user_input == "/undo" or user_input.startswith("/undo "):
                try:
                    turn_id = user_input[len("/undo"):].strip()
                    _apply_changeset_action(agent, "undo", turn_id)
                except (OSError, RuntimeError, ValueError) as exc:
                    console.print(f"[yellow]Undo unavailable: {exc}[/yellow]")
                continue
            if user_input == "/reapply" or user_input.startswith("/reapply "):
                try:
                    turn_id = user_input[len("/reapply"):].strip()
                    _apply_changeset_action(agent, "reapply", turn_id)
                except (OSError, RuntimeError, ValueError) as exc:
                    console.print(f"[yellow]Reapply unavailable: {exc}[/yellow]")
                continue

            worker.start_chat(user_input)


def _last_editable_prompt(agent: Agent) -> tuple[str, str]:
    task = agent.task_state
    if task is None or task.status != "completed":
        raise ValueError("Only the last completed turn can be edited.")
    for message in reversed(agent.messages):
        if (
            message.get("role") == "user"
            and message.get("message_kind", "prompt") == "prompt"
            and message.get("turn_id", task.task_id) == task.task_id
        ):
            return task.task_id, str(
                message.get("raw_prompt") or content_text(message.get("content", ""))
            )
    raise ValueError(f"Prompt for turn '{task.task_id}' was not found.")


def _apply_changeset_action(agent: Agent, action: str, turn_id: str = ""):
    """Small CLI adapter around the state-layer ChangeSet API."""
    session = agent.session_state
    task = agent.task_state
    if session is None:
        raise ValueError("No active session.")
    resolved_turn_id = turn_id or (task.task_id if task is not None else "")
    if not resolved_turn_id:
        raise ValueError("Turn id is required.")
    store = ChangeSetStore(agent.workspace_root, session.session_id)
    operation = getattr(store, action)
    manifest = operation(resolved_turn_id)
    console.print(
        f"[green]{action.title()} complete for [cyan]{resolved_turn_id}[/cyan] "
        f"({len(manifest.changed_paths)} files).[/green]"
    )
    return manifest


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
        "  /edit-last     Edit and rerun the last completed prompt\n"
        "  /undo [turn]   Undo one turn's workspace changes\n"
        "  /reapply [turn] Reapply a previously undone turn\n"
        "  /approve       Approve the pending tool call\n"
        "  /approve_scope Approve and allow this scope for the current task\n"
        "  /permissions   Set ask or full_access tool permissions\n"
        "  /reject        Reject the pending tool call\n"
        "  quit           Exit AutoCode\n"
        "\n"
        "[bold]Input:[/bold]\n"
        "  Enter          Submit; while running, steer the active turn\n"
        "  Tab            While running, queue the next turn\n"
        "  Esc+Enter      Insert newline (for pasting code)",
        title="AutoCode Help",
        border_style="dim",
    ))


def _brief(kwargs: dict, maxlen: int = 80) -> str:
    s = ", ".join(f"{k}={repr(v)[:40]}" for k, v in kwargs.items())
    return s[:maxlen] + ("..." if len(s) > maxlen else "")


def _format_token_count(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def _context_toolbar(agent: Agent, running: bool = False):
    usage = agent.context_usage()
    status = " · running: Enter steer / Tab queue" if running else ""
    return [
        ("class:bottom-toolbar", " Context "),
        ("class:bottom-toolbar.text", f"{usage['used_percent']:.1f}% used"),
        (
            "class:bottom-toolbar",
            f" · {_format_token_count(usage['used_tokens'])} / "
            f"{_format_token_count(usage['window_tokens'])} tokens{status} ",
        ),
    ]


def _resume_candidates(workspace_root: str, limit: int = 10) -> list[dict]:
    return list_sessions(workspace_root=workspace_root, limit=limit)


def _load_resumable_session(session_id: str, workspace_root: str):
    allowed = {item["session_id"] for item in _resume_candidates(workspace_root, limit=200)}
    if session_id not in allowed:
        return None
    return load_checkpoint(session_id)


def _render_conversation_history(messages: list[dict]) -> None:
    visible = [
        message for message in messages
        if message.get("role") in {"user", "assistant"}
        and not is_internal_visual_context(message.get("content"))
        and content_text(message.get("content", "")).strip()
    ]
    if not visible:
        console.print("[dim]This session has no saved conversation messages.[/dim]")
        return
    console.print(f"[dim]Conversation history ({len(visible)} messages)[/dim]")
    for message in visible:
        role = "You" if message["role"] == "user" else "AutoCode"
        color = "bright_blue" if message["role"] == "user" else "green"
        console.print(
            Panel(
                Markdown(content_text(message.get("content", ""))),
                title=role,
                title_align="left",
                border_style=color,
            )
        )


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
        if result == "/approve_scope":
            return "approve_scope"
        if result == "/reject":
            return False
        console.print("[yellow]Choose /approve, /approve_scope, /reject, or /later.[/yellow]")

