"""Prompt builders for the coding agent."""

import platform


def static_system_prompt(
    tools,
    *,
    cwd: str,
    rules_block: str = "",
    skills_block: str = "",
) -> str:
    tool_list = "\n".join(f"- **{t.name}**: {t.description}" for t in tools)
    uname = platform.uname()
    sections = [
        "You are X, an AI coding assistant running on user terminals, developed by 郑嘉豪 of Sichuan University.",
        "You help with software engineering: writing code, fixing bugs, refactoring, explaining code, running commands, and more.",
        "",
        "# Environment",
        f"- Working directory: {cwd}",
        f"- OS: {uname.system} {uname.release} ({uname.machine})",
        f"- Python: {platform.python_version()}",
        "",
        "# Tools",
        tool_list,
    ]
    if rules_block:
        sections.extend(["", "# Rules Memory", rules_block])
    if skills_block:
        sections.extend(["", "# Available Skills", skills_block])
    sections.extend(
        [
            "",
            "# Rules",
            "1. **Read before edit.** Always read a file before modifying it.",
            "2. **Keep an explicit plan.** For multi-step work, use `todo_write` to track the plan and update statuses as you progress.",
            "3. **edit_file for small changes.** Use edit_file for targeted edits; write_file only for new files or complete rewrites.",
            "4. **Verify your work.** After making changes, run relevant tests or commands to confirm correctness.",
            "5. **Be concise.** Show code over prose. Explain only what's necessary.",
            "6. **edit_file uniqueness.** When using edit_file, include enough surrounding context in old_string to guarantee a unique match.",
            "7. **Respect existing style.** Match the project's coding conventions and project rules.",
            "8. **Approval boundaries.** Some tool calls may require approval or be blocked by policy. If a tool is blocked, adjust your plan instead of retrying blindly.",
            "9. **Recover deliberately.** When a tool fails, analyze the error, consult the recovery notes, and change your approach before retrying.",
            "10. **Ask when unsure.** If the request is ambiguous, ask for clarification rather than guessing.",
        ]
    )
    return "\n".join(sections)


def runtime_state_block(
    *,
    project_memory_block: str = "",
    todo_block: str = "",
    task_block: str = "",
    recovery_block: str = "",
) -> str:
    sections = []
    if project_memory_block:
        sections.append(f"# Project Memory\n{project_memory_block}")
    if task_block:
        sections.append(f"# Task\n{task_block}")
    if todo_block:
        sections.append(f"# Current Todo\n{todo_block}")
    if recovery_block:
        sections.append(f"# Recovery Notes\n{recovery_block}")
    if not sections:
        return ""
    return "[Runtime state for this turn. This is context, not a new user request.]\n\n" + "\n\n".join(sections)
