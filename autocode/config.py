"""Configuration - env vars and defaults."""

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv():
    """Load the nearest .env from the current workspace tree."""
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
    except ImportError:
        pass  # python-dotenv not installed, silently skip


@dataclass
class Config:
    model: str = ""
    api_key: str = ""
    base_url: str | None = None
    max_tokens: int = 4096
    temperature: float = 0.0
    max_context_tokens: int = 1_000_000
    provider: str = "openai"
    workspace_root: str = ""
    auto_approve: bool = False
    telegram_bot_token: str = ""
    telegram_allowed_chat_ids: tuple[int, ...] = ()
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_allowed_open_ids: tuple[str, ...] = ()
    feishu_allowed_chat_ids: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "Config":
        # load .env if present (won't override existing env vars)
        _load_dotenv()
        return cls(
            model=os.getenv("AUTOCODE_MODEL", ""),
            api_key=os.getenv("AUTOCODE_API_KEY", ""),
            base_url=os.getenv("AUTOCODE_BASE_URL"),
            max_tokens=int(os.getenv("AUTOCODE_MAX_TOKENS", "4096")),
            temperature=float(os.getenv("AUTOCODE_TEMPERATURE", "0")),
            max_context_tokens=int(os.getenv("AUTOCODE_MAX_CONTEXT", "1000000")),
            provider=os.getenv("AUTOCODE_PROVIDER", "openai"),
            workspace_root=os.getenv("AUTOCODE_WORKSPACE_ROOT", str(Path.cwd())),
            auto_approve=os.getenv("AUTOCODE_AUTO_APPROVE", "").lower() in {"1", "true", "yes", "on"},
            telegram_bot_token=os.getenv("AUTOCODE_TELEGRAM_BOT_TOKEN", ""),
            telegram_allowed_chat_ids=_parse_chat_ids(os.getenv("AUTOCODE_TELEGRAM_ALLOWED_CHATS", "")),
            feishu_app_id=os.getenv("AUTOCODE_FEISHU_APP_ID", ""),
            feishu_app_secret=os.getenv("AUTOCODE_FEISHU_APP_SECRET", ""),
            feishu_allowed_open_ids=_parse_csv(os.getenv("AUTOCODE_FEISHU_ALLOWED_OPEN_IDS", "")),
            feishu_allowed_chat_ids=_parse_csv(os.getenv("AUTOCODE_FEISHU_ALLOWED_CHAT_IDS", "")),
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


def _parse_csv(raw: str) -> tuple[str, ...]:
    return tuple(token.strip() for token in raw.split(",") if token.strip())

