"""Configuration - env vars and defaults."""

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv_values() -> dict[str, str]:
    """Read the nearest .env from the current workspace tree."""
    try:
        from dotenv import dotenv_values
    except ImportError:
        return {}

    cur = Path.cwd()
    home = Path.home()
    while True:
        candidate = cur / ".env"
        if candidate.exists():
            values = dotenv_values(candidate)
            return {
                key: value
                for key, value in values.items()
                if isinstance(key, str) and isinstance(value, str) and value
            }
        if cur == home or cur == cur.parent:
            return {}
        cur = cur.parent


def _resolve_config_value(snapshot: dict[str, str], key: str, default: str = "") -> str:
    return snapshot.get(key) or os.getenv(key, default)


@dataclass
class Config:
    model: str = ""
    api_key: str = ""
    base_url: str | None = None
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_base_url: str | None = None
    tavily_api_key: str = ""
    max_tokens: int = 4096
    temperature: float = 0.0
    max_context_tokens: int = 1_000_000
    provider: str = "openai"
    workspace_root: str = ""
    auto_approve: bool = False
    mcp_config_path: str = ""
    telegram_bot_token: str = ""
    telegram_allowed_chat_ids: tuple[int, ...] = ()
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_allowed_open_ids: tuple[str, ...] = ()
    feishu_allowed_chat_ids: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "Config":
        snapshot = _load_dotenv_values()
        return cls(
            model=_resolve_config_value(snapshot, "AUTOCODE_MODEL"),
            api_key=_resolve_config_value(snapshot, "AUTOCODE_API_KEY"),
            base_url=_resolve_config_value(snapshot, "AUTOCODE_BASE_URL") or None,
            langfuse_public_key=_resolve_config_value(snapshot, "LANGFUSE_PUBLIC_KEY"),
            langfuse_secret_key=_resolve_config_value(snapshot, "LANGFUSE_SECRET_KEY"),
            langfuse_base_url=_resolve_config_value(snapshot, "LANGFUSE_BASE_URL") or None,
            tavily_api_key=_resolve_config_value(snapshot, "TAVILY_API_KEY"),
            max_tokens=int(_resolve_config_value(snapshot, "AUTOCODE_MAX_TOKENS", "4096")),
            temperature=float(_resolve_config_value(snapshot, "AUTOCODE_TEMPERATURE", "0")),
            max_context_tokens=int(_resolve_config_value(snapshot, "AUTOCODE_MAX_CONTEXT", "1000000")),
            provider=_resolve_config_value(snapshot, "AUTOCODE_PROVIDER", "openai"),
            workspace_root=_resolve_config_value(snapshot, "AUTOCODE_WORKSPACE_ROOT", str(Path.cwd())),
            auto_approve=_resolve_config_value(snapshot, "AUTOCODE_AUTO_APPROVE", "").lower() in {"1", "true", "yes", "on"},
            mcp_config_path=_resolve_config_value(snapshot, "AUTOCODE_MCP_CONFIG"),
            telegram_bot_token=_resolve_config_value(snapshot, "AUTOCODE_TELEGRAM_BOT_TOKEN"),
            telegram_allowed_chat_ids=_parse_chat_ids(_resolve_config_value(snapshot, "AUTOCODE_TELEGRAM_ALLOWED_CHATS", "")),
            feishu_app_id=_resolve_config_value(snapshot, "AUTOCODE_FEISHU_APP_ID"),
            feishu_app_secret=_resolve_config_value(snapshot, "AUTOCODE_FEISHU_APP_SECRET"),
            feishu_allowed_open_ids=_parse_csv(_resolve_config_value(snapshot, "AUTOCODE_FEISHU_ALLOWED_OPEN_IDS", "")),
            feishu_allowed_chat_ids=_parse_csv(_resolve_config_value(snapshot, "AUTOCODE_FEISHU_ALLOWED_CHAT_IDS", "")),
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

