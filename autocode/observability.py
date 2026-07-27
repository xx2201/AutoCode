"""Optional Langfuse tracing helpers."""

from __future__ import annotations

import contextvars
import logging
from contextlib import contextmanager
from typing import Any

from .diagnostics import get_diagnostic_logger, log_event


_ACTIVE_OBSERVATION_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar(
    "autocode_langfuse_active_observation_depth",
    default=0,
)


def _clean_mapping(data: dict[str, Any] | None) -> dict[str, Any] | None:
    if not data:
        return None
    cleaned = {key: value for key, value in data.items() if value is not None and value != ""}
    return cleaned or None


class _NoopObservation:
    trace_id = None
    id = None

    def update(self, **kwargs):
        return None


class LangfuseTracer:
    """Official Langfuse SDK integration with background batching."""

    def __init__(
        self,
        public_key: str = "",
        secret_key: str = "",
        base_url: str | None = None,
    ):
        self.public_key = public_key or ""
        self.secret_key = secret_key or ""
        self.base_url = base_url
        self._client = None
        self._shutdown = False
        self._logger = get_diagnostic_logger("observability")
        self._status = "disabled"
        self._last_error = ""
        if not (self.public_key and self.secret_key):
            log_event(self._logger, logging.INFO, "Langfuse disabled: credentials not configured")
            return
        try:
            from langfuse import Langfuse
        except ImportError as exc:
            self._status = "error"
            self._last_error = "Langfuse credentials are configured but the official SDK is not installed."
            log_event(
                self._logger,
                logging.ERROR,
                "Langfuse SDK import failed",
                error=self._last_error,
            )
            raise RuntimeError(self._last_error) from exc
        kwargs = {
            "public_key": self.public_key,
            "secret_key": self.secret_key,
        }
        if self.base_url:
            kwargs["base_url"] = self.base_url
        try:
            self._client = Langfuse(**kwargs)
        except Exception as exc:
            self._status = "error"
            self._last_error = f"{type(exc).__name__}: {exc}"
            log_event(
                self._logger,
                logging.ERROR,
                "Langfuse client initialization failed",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise
        self._status = "enabled"
        log_event(
            self._logger,
            logging.INFO,
            "Langfuse client initialized",
            base_url=self.base_url or "https://cloud.langfuse.com",
            batching="background",
        )

    @property
    def enabled(self) -> bool:
        return self._client is not None

    @property
    def status(self) -> dict[str, Any]:
        return {
            "provider": "langfuse",
            "configured": bool(self.public_key and self.secret_key),
            "enabled": self.enabled,
            "status": self._status,
            "base_url": self.base_url or "",
            "last_error": self._last_error,
            "delivery": "background-batched" if self.enabled else "disabled",
        }

    @contextmanager
    def _observation_scope(self):
        token = _ACTIVE_OBSERVATION_DEPTH.set(_ACTIVE_OBSERVATION_DEPTH.get() + 1)
        try:
            yield
        finally:
            _ACTIVE_OBSERVATION_DEPTH.reset(token)

    @contextmanager
    def start_agent_turn(
        self,
        *,
        name: str,
        input_payload: Any,
        session_id: str = "",
        trace_name: str = "",
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        trace_context: dict[str, str] | None = None,
    ):
        if not self.enabled:
            yield _NoopObservation()
            return

        metadata = _clean_mapping(metadata)
        tags = [tag for tag in (tags or []) if tag]
        nested = _ACTIVE_OBSERVATION_DEPTH.get() > 0
        observation_kwargs = {
            "name": name,
            "as_type": "agent",
            "input": input_payload,
        }
        if trace_context and not nested:
            observation_kwargs["trace_context"] = trace_context
        if nested:
            with self._observation_scope():
                with self._client.start_as_current_observation(**observation_kwargs) as observation:
                    if metadata:
                        observation.update(metadata=metadata)
                    yield observation
            return

        try:
            from langfuse import propagate_attributes
        except ImportError:
            propagate_attributes = None

        propagation = (
            propagate_attributes(
                session_id=session_id or None,
                metadata=metadata,
                tags=tags or None,
                trace_name=trace_name or None,
            )
            if propagate_attributes is not None
            else None
        )
        if propagation is None:
            with self._observation_scope():
                with self._client.start_as_current_observation(**observation_kwargs) as observation:
                    if metadata:
                        observation.update(metadata=metadata)
                    yield observation
            return

        with propagation:
            with self._observation_scope():
                with self._client.start_as_current_observation(**observation_kwargs) as observation:
                    if metadata:
                        observation.update(metadata=metadata)
                    yield observation

    @contextmanager
    def start_generation(
        self,
        *,
        name: str,
        input_payload: Any,
        model: str,
        model_parameters: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        if not self.enabled:
            yield _NoopObservation()
            return

        with self._observation_scope():
            with self._client.start_as_current_observation(
                name=name,
                as_type="generation",
                input=input_payload,
                model=model,
                model_parameters=model_parameters or None,
            ) as observation:
                metadata = _clean_mapping(metadata)
                if metadata:
                    observation.update(metadata=metadata)
                yield observation

    @contextmanager
    def start_tool(
        self,
        *,
        name: str,
        input_payload: Any,
        metadata: dict[str, Any] | None = None,
    ):
        if not self.enabled:
            yield _NoopObservation()
            return

        with self._observation_scope():
            with self._client.start_as_current_observation(
                name=name,
                as_type="tool",
                input=input_payload,
            ) as observation:
                metadata = _clean_mapping(metadata)
                if metadata:
                    observation.update(metadata=metadata)
                yield observation

    def flush(self) -> None:
        """Force immediate delivery for tests or short-lived scripts."""
        if not self.enabled:
            return
        try:
            self._client.flush()
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            log_event(
                self._logger,
                logging.ERROR,
                "Langfuse flush failed",
                error_type=type(exc).__name__,
                error=str(exc),
            )

    def shutdown(self) -> None:
        """Drain the SDK queue when the owning runtime exits."""
        if not self.enabled or self._shutdown:
            return
        self._shutdown = True
        try:
            self._client.shutdown()
            self._status = "shutdown"
            log_event(self._logger, logging.INFO, "Langfuse client shut down")
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            log_event(
                self._logger,
                logging.ERROR,
                "Langfuse shutdown failed",
                error_type=type(exc).__name__,
                error=str(exc),
            )
