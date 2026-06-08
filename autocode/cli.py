"""Interactive REPL - the user-facing terminal interface."""

import sys
import os
import argparse

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings

from .agent import Agent
from .config import Config
from .context import render_todos
from .llm import LLM, LiteLLM
from .state import (
    format_trace,
    list_checkpoints,
    list_sessions,
    load_checkpoint,
    load_session,
    load_trace,
    save_session,
)
from . import __version__

console = Console()


def _parse_args():
    p = argparse.ArgumentParser(
        prog="autocode",
        description="Minimal AI coding agent. Works with any OpenAI-compatible LLM.",
    )
    p.add_argument("-m", "--model", help="Model name (default: current configured model)")
    p.add_argument("--base-url", help="API base URL (default: current configured base URL)")
    p.add_argument("--api-key", help="API key (default: current configured API key)")
    p.add_argument("-p", "--prompt", help="One-shot prompt (non-interactive mode)")
    p.add_argument("-r", "--resume", metavar="ID", help="Resume a saved session")
    p.add_argument("--resume-task", metavar="ID", help="Resume an in-flight task checkpoint")
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
            "Set `AUTOCODE_MODEL`, pass `-m/--model`, or configure the agent repo `.env`.\n"
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

    llm_cls = LiteLLM if config.provider == "litellm" else LLM
    llm = llm_cls(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
    )
    agent = Agent(
        llm=llm,
        max_context_tokens=config.max_context_tokens,
        workspace_root=config.workspace_root,
        auto_approve=config.auto_approve,
    )

    # resume saved session
    if args.resume:
        loaded = load_session(args.resume)
        if loaded:
            agent.messages, loaded_model = loaded
            # restore the model from the saved session unless overridden by CLI
            if not args.model:
                agent.llm.model = loaded_model
                config.model = loaded_model
            console.print(f"[green]Resumed session: {args.resume} (model: {agent.llm.model})[/green]")
        else:
            console.print(f"[red]Session '{args.resume}' not found.[/red]")
            sys.exit(1)

    if args.resume_task:
        loaded = load_checkpoint(args.resume_task)
        if loaded:
            task_state, messages, loaded_model = loaded
            agent.restore_task(task_state, messages, loaded_model)
            if not args.model:
                agent.llm.model = loaded_model
                config.model = loaded_model
            console.print(
                f"[green]Resumed task: {task_state.task_id} "
                f"(status: {task_state.status}, model: {agent.llm.model})[/green]"
            )
        else:
            console.print(f"[red]Task '{args.resume_task}' not found.[/red]")
            sys.exit(1)

    # one-shot mode
    if args.prompt:
        _run_once(agent, args.prompt)
        return

    # interactive REPL
    _repl(agent, config, session_id=args.resume)


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


def _repl(agent: Agent, config: Config, session_id: str | None = None):
    """Interactive read-eval-print loop."""
    current_model = agent.llm.model
    console.print(Panel(
        f"[bold]AutoCode[/bold] v{__version__}\n"
        f"Model: [cyan]{current_model}[/cyan]"
        + (f"  Base: [dim]{config.base_url}[/dim]" if config.base_url else "")
        + "\nType [bold]/help[/bold] for commands, [bold]Ctrl+C[/bold] to cancel, [bold]quit[/bold] to exit.",
        border_style="blue",
    ))
    session_id = _autosave_session(agent.messages, config.model, session_id)

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
        nonlocal session_id
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
        session_id = _autosave_session(agent.messages, config.model, session_id)

    while True:
        try:
            user_input = pt_prompt(
                "You > ",
                history=history,
                multiline=True,
                key_bindings=kb,
                prompt_continuation="...  ",
            ).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nBye!")
            break

        if not user_input:
            continue

        # built-in commands
        if user_input.lower() in ("quit", "exit", "/quit", "/exit"):
            break
        if user_input == "/help":
            _show_help()
            continue
        if user_input == "/reset":
            agent.reset()
            session_id = None
            console.print("[yellow]Conversation reset.[/yellow]")
            continue
        if user_input == "/tokens":
            p = agent.llm.total_prompt_tokens
            c = agent.llm.total_completion_tokens
            line = f"Tokens: [cyan]{p}[/cyan] prompt + [cyan]{c}[/cyan] completion = [bold]{p+c}[/bold] total"
            cost = agent.llm.estimated_cost
            if cost is not None:
                line += f"  (~${cost:.4f})"
            console.print(line)
            continue
        if user_input == "/model" or user_input.startswith("/model "):
            new_model = user_input[7:].strip() if user_input.startswith("/model ") else ""
            if new_model:
                agent.llm.model = new_model
                config.model = new_model
                session_id = _autosave_session(agent.messages, config.model, session_id)
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
        if user_input == "/sessions":
            sessions = list_sessions()
            if not sessions:
                console.print("[dim]No saved sessions.[/dim]")
            else:
                for s in sessions:
                    console.print(f"  [cyan]{s['id']}[/cyan] ({s['model']}, {s['saved_at']}) {s['preview']}")
            continue
        if user_input == "/task":
            if agent.task_state is None:
                console.print("[dim]No active task.[/dim]")
            else:
                pending = ""
                if agent.task_state.pending_approval:
                    pending = f"  pending: {agent.task_state.pending_approval.tool_name}"
                auto = "on" if agent.task_state.auto_approve_for_task else "off"
                console.print(
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
        if user_input == "/tasks":
            tasks = list_checkpoints()
            if not tasks:
                console.print("[dim]No task checkpoints.[/dim]")
            else:
                for t in tasks:
                    console.print(
                        f"  [cyan]{t['task_id']}[/cyan] ({t['status']}, step {t['step_index']}, "
                        f"{t['model']}, {t['saved_at']})"
                    )
            continue
        if user_input == "/trace":
            if agent.task_state is None:
                console.print("[dim]No active task.[/dim]")
                continue
            trace = load_trace(agent.task_state.task_id)
            if trace is None:
                console.print("[dim]No trace recorded yet.[/dim]")
            else:
                console.print(Panel(format_trace(trace), title="Trace", border_style="dim"))
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
                approval_handler=None if config.auto_approve else _prompt_approval,
            )
            session_id = _autosave_session(agent.messages, config.model, session_id)
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
        "  /sessions      List saved sessions\n"
        "  /task          Show the current task state\n"
        "  /todo          Show the current todo list\n"
        "  /tasks         List task checkpoints\n"
        "  /trace         Show the current task trace\n"
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


def _autosave_session(messages: list[dict], model: str, session_id: str | None) -> str | None:
    if not messages:
        return session_id
    return save_session(messages, model, session_id)


def _prompt_approval(pending) -> str | bool:
    command_line = ""
    if getattr(pending, "tool_name", "") == "bash":
        command = getattr(pending, "arguments", {}).get("command", "")
        if command:
            command_line = f"\nCommand: [dim]{command}[/dim]"
    prompt = (
        f"\nApprove tool call [cyan]{pending.tool_name}[/cyan] "
        f"because: {pending.reason or 'confirmation required'}?"
        f"{command_line}\n[y/N or /approve /approve_all /reject] "
    )
    console.print(prompt, end="")
    choice = input().strip().lower()
    if choice in {"y", "yes", "/approve"}:
        return "approve"
    if choice == "/approve_all":
        return "approve_all"
    return False

