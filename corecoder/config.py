"""Configuration - env vars and defaults."""

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv():
    """Load .env from cwd first, then fall back to the agent repo root."""
    try:
        from dotenv import load_dotenv

        cur = Path.cwd()
        home = Path.home()
        while True:
            candidate = cur / ".env"
            if candidate.exists():
                load_dotenv(candidate, override=False)
                break
            if cur == home or cur == cur.parent:
                break
            cur = cur.parent

        # When CoreCoder is launched from another project, still allow the
        # agent's own repo-level .env to provide defaults.
        repo_env = Path(__file__).resolve().parent.parent / ".env"
        if repo_env.exists():
            load_dotenv(repo_env, override=False)
    except ImportError:
        pass  # python-dotenv not installed, silently skip


@dataclass
class Config:
    model: str = ""
    api_key: str = ""
    base_url: str | None = None
    max_tokens: int = 4096
    temperature: float = 0.0
    max_context_tokens: int = 128_000
    provider: str = "openai"
    workspace_root: str = ""
    auto_approve: bool = False
    telegram_bot_token: str = ""
    telegram_allowed_chat_ids: tuple[int, ...] = ()

    @classmethod
    def from_env(cls) -> "Config":
        # load .env if present (won't override existing env vars)
        _load_dotenv()
        return cls(
            model=os.getenv("CORECODER_MODEL", ""),
            api_key=os.getenv("CORECODER_API_KEY", ""),
            base_url=os.getenv("CORECODER_BASE_URL"),
            max_tokens=int(os.getenv("CORECODER_MAX_TOKENS", "4096")),
            temperature=float(os.getenv("CORECODER_TEMPERATURE", "0")),
            max_context_tokens=int(os.getenv("CORECODER_MAX_CONTEXT", "128000")),
            provider=os.getenv("CORECODER_PROVIDER", "openai"),
            workspace_root=os.getenv("CORECODER_WORKSPACE_ROOT", str(Path.cwd())),
            auto_approve=os.getenv("CORECODER_AUTO_APPROVE", "").lower() in {"1", "true", "yes", "on"},
            telegram_bot_token=os.getenv("CORECODER_TELEGRAM_BOT_TOKEN", ""),
            telegram_allowed_chat_ids=_parse_chat_ids(os.getenv("CORECODER_TELEGRAM_ALLOWED_CHATS", "")),
        )


def _parse_chat_ids(raw: str) -> tuple[int, ...]:
    values: list[int] = []
    for item in raw.split(","):
        token = item.strip()
        if not token:
            continue
        try:
            values.append(int(token))
        except ValueError:
            continue
    return tuple(values)
