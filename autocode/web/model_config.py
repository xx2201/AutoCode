"""Validation and persistence for the model settings used by the web Runner."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from ..config import Config
from ..llm import api_format_for_provider


_LOGGER = logging.getLogger(__name__)
_MODEL_CONFIG_ENV = "AUTOCODE_MODEL_CONFIG_PATH"
_DEFAULT_MODEL_CONFIG_PATH = Path.home() / ".autocode" / "web-model.json"
SUPPORTED_PROVIDERS = ("anthropic", "openai", "litellm")


def normalize_model_config(
    *,
    model: str,
    api_key: str,
    base_url: str | None,
    provider: str,
) -> dict[str, str | None]:
    """Normalize and validate the four model settings exposed by the web UI."""
    normalized_model = str(model or "").strip()
    if not normalized_model:
        raise ValueError("模型名称不能为空。")
    if len(normalized_model) > 200:
        raise ValueError("模型名称不能超过 200 个字符。")

    normalized_api_key = str(api_key or "").strip()
    if not normalized_api_key:
        raise ValueError("API Key 不能为空。")
    if len(normalized_api_key) > 4096:
        raise ValueError("API Key 不能超过 4096 个字符。")

    normalized_provider = str(provider or "").strip().lower()
    if normalized_provider not in SUPPORTED_PROVIDERS:
        choices = ", ".join(SUPPORTED_PROVIDERS)
        raise ValueError(f"不支持的模型协议，请选择：{choices}。")
    # Resolve the adapter while validating so the stored protocol cannot point
    # at a provider the installed runtime does not understand.
    api_format_for_provider(normalized_provider)

    normalized_base_url = str(base_url or "").strip()
    if normalized_base_url:
        parsed = urlsplit(normalized_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("URL 必须以 http:// 或 https:// 开头，并包含主机名。")
        if len(normalized_base_url) > 2000:
            raise ValueError("URL 不能超过 2000 个字符。")

    return {
        "model": normalized_model,
        "api_key": normalized_api_key,
        "base_url": normalized_base_url or None,
        "provider": normalized_provider,
    }


def public_model_config(config: Config) -> dict[str, str | bool]:
    """Return model settings safe to send to the browser."""
    return {
        "model": config.model,
        "provider": config.provider,
        "api_format": api_format_for_provider(config.provider),
        "base_url": config.base_url or "",
        "api_key_configured": bool(config.api_key),
    }


class ModelConfigStore:
    """Persist only the web-editable model settings outside the repository."""

    def __init__(self, path: str | Path | None = None):
        configured = str(path or os.getenv(_MODEL_CONFIG_ENV, "")).strip()
        self.path = (
            Path(configured).expanduser()
            if configured
            else _DEFAULT_MODEL_CONFIG_PATH
        )

    def apply(self, config: Config) -> Config:
        """Apply a valid saved override, retaining environment values if absent."""
        data = self._read()
        if data is None:
            return config
        try:
            normalized = normalize_model_config(
                model=data.get("model", config.model),
                api_key=data.get("api_key", config.api_key),
                base_url=data.get("base_url", config.base_url),
                provider=data.get("provider", config.provider),
            )
        except (TypeError, ValueError) as exc:
            _LOGGER.warning("Ignoring invalid saved web model config: %s", exc)
            return config
        return replace(config, **normalized)

    def save(self, config: Config) -> None:
        """Atomically write the four settings and keep the file private."""
        payload = {
            "model": config.model,
            "api_key": config.api_key,
            "base_url": config.base_url,
            "provider": config.provider,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                # Windows ACLs are managed by the user profile; chmod is best effort there.
                pass
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _read(self) -> dict | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            _LOGGER.warning("Ignoring unreadable saved web model config: %s", exc)
            return None
        if not isinstance(data, dict):
            _LOGGER.warning("Ignoring saved web model config with a non-object root")
            return None
        return data
