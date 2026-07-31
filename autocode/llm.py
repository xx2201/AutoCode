"""Provider adapters for native Anthropic Messages and Chat Completions.

Anthropic Messages is the default protocol. OpenAI-compatible providers remain
available through ``AUTOCODE_PROVIDER=openai``; LiteLLM is an optional third
adapter for provider-specific deployments.
"""

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from random import random

from anthropic import (
    Anthropic,
    APIConnectionError as AnthropicAPIConnectionError,
    APITimeoutError as AnthropicAPITimeoutError,
    APIStatusError as AnthropicAPIStatusError,
    RateLimitError as AnthropicRateLimitError,
)
from openai import OpenAI, APIError, RateLimitError, APITimeoutError, APIConnectionError

from .config import DEFAULT_MAX_OUTPUT_TOKENS
from .observability import LangfuseTracer


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
    raw_arguments: str | None = None
    parse_error: str | None = None


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_miss_tokens: int = 0
    stop_reason: str = ""

    @property
    def message(self) -> dict:
        """Convert to OpenAI message format for appending to history."""
        msg: dict = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": (
                            tc.raw_arguments
                            if tc.parse_error and tc.raw_arguments is not None
                            else json.dumps(tc.arguments)
                        ),
                    },
                }
                for tc in self.tool_calls
            ]
        return msg


def _parse_tool_arguments(raw_args: str) -> tuple[dict, str | None]:
    if not raw_args:
        return {}, "tool-call arguments were empty"
    try:
        parsed = json.loads(raw_args)
    except json.JSONDecodeError as exc:
        return {}, f"tool-call arguments were not valid JSON: {exc.msg} at char {exc.pos}"
    if not isinstance(parsed, dict):
        return {}, f"tool-call arguments must decode to a JSON object, got {type(parsed).__name__}"
    return parsed, None


def _usage_field(obj, name: str):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _field(obj, name: str):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _usage_int(obj, name: str) -> int:
    value = _usage_field(obj, name)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _extract_cache_usage(usage) -> tuple[int, int]:
    hit = _usage_int(usage, "prompt_cache_hit_tokens")
    miss = _usage_int(usage, "prompt_cache_miss_tokens")
    details = _usage_field(usage, "prompt_tokens_details")
    cached = _usage_int(details, "cached_tokens")
    prompt_tokens = _usage_int(usage, "prompt_tokens")
    has_cache_metadata = bool(hit or miss or cached or details is not None)
    if hit == 0 and cached:
        hit = cached
    if miss == 0 and has_cache_metadata and prompt_tokens:
        miss = max(prompt_tokens - hit, 0)
    return hit, miss


def _langfuse_usage(response: LLMResponse) -> dict[str, int]:
    """Return mutually exclusive Langfuse usage buckets."""
    has_cache_breakdown = bool(response.cache_read_tokens or response.cache_miss_tokens)
    usage = {
        "input": (
            response.cache_miss_tokens
            if has_cache_breakdown
            else response.prompt_tokens
        ),
        "output": response.completion_tokens,
    }
    if response.cache_read_tokens:
        usage["cache_read_input_tokens"] = response.cache_read_tokens
    return usage


# pricing per million tokens: (input, output)
# sources: openai.com/api/pricing, api-docs.deepseek.com, platform.claude.com,
#          platform.moonshot.ai, alibabacloud.com/help/en/model-studio
_PRICING = {
    # OpenAI - current flagships
    "gpt-5.4": (2.5, 15),
    "gpt-5.4-mini": (0.75, 4.5),
    "gpt-5.4-nano": (0.2, 1.25),
    "o4-mini": (1.1, 4.4),
    # OpenAI - previous gen (still widely used)
    "gpt-4.1": (2, 8),
    "gpt-4.1-mini": (0.4, 1.6),
    "gpt-4.1-nano": (0.1, 0.4),
    "gpt-4o": (2.5, 10),
    "gpt-4o-mini": (0.15, 0.6),
    # DeepSeek
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
    # Anthropic Claude
    "claude-opus-4-6": (5, 25),
    "claude-sonnet-4-6": (3, 15),
    "claude-haiku-4-5": (1, 5),
    # Alibaba Qwen
    "qwen3-max": (0.78, 3.9),
    "qwen3-plus": (0.26, 0.78),
    "qwen-max": (0.78, 3.9),
    # Moonshot Kimi
    "kimi-k2.5": (0.6, 3),
}


class LLM:
    api_format = "chat_completions"
    supports_streaming_tool_calls = False

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        langfuse_public_key: str = "",
        langfuse_secret_key: str = "",
        langfuse_base_url: str | None = None,
        tracer: LangfuseTracer | None = None,
        **kwargs,
    ):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.langfuse_public_key = langfuse_public_key
        self.langfuse_secret_key = langfuse_secret_key
        self.langfuse_base_url = langfuse_base_url
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.extra = kwargs  # temperature, max_tokens, etc.
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_cache_read_tokens = 0
        self.total_cache_miss_tokens = 0
        self.tracer = tracer or LangfuseTracer(
            public_key=langfuse_public_key,
            secret_key=langfuse_secret_key,
            base_url=langfuse_base_url,
        )

    def clone(self) -> "LLM":
        return type(self)(
            model=self.model,
            api_key=self.api_key,
            base_url=self.base_url,
            langfuse_public_key=self.langfuse_public_key,
            langfuse_secret_key=self.langfuse_secret_key,
            langfuse_base_url=self.langfuse_base_url,
            tracer=self.tracer,
            **self.extra,
        )

    @property
    def estimated_cost(self) -> float | None:
        """Rough cost estimate in USD. Returns None if model not in pricing table."""
        pricing = _PRICING.get(self.model)
        if not pricing:
            return None
        input_rate, output_rate = pricing
        return (
            self.total_prompt_tokens * input_rate / 1_000_000
            + self.total_completion_tokens * output_rate / 1_000_000
        )

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_token=None,
    ) -> LLMResponse:
        """Send messages, stream back response, handle tool calls."""
        params: dict = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            **self.extra,
        }
        if tools:
            params["tools"] = tools

        with self.tracer.start_generation(
            name="llm.chat",
            input_payload={"messages": messages, "tools": tools or []},
            model=self.model,
            model_parameters=self.extra,
            metadata={
                "backend": type(self).__name__,
                "tool_schema_count": len(tools or []),
            },
        ) as generation:
            try:
                # stream_options is an OpenAI extension; not all providers support it
                try:
                    params["stream_options"] = {"include_usage": True}
                    stream = self._call_with_retry(params)
                except Exception:
                    params.pop("stream_options", None)
                    stream = self._call_with_retry(params)

                content_parts: list[str] = []
                tc_map: dict[int, dict] = {}  # index -> {id, name, arguments_str}
                prompt_tok = 0
                completion_tok = 0
                cache_read_tok = 0
                cache_miss_tok = 0
                completion_started = False
                stop_reason = ""

                for chunk in stream:
                    # usage info comes in the final chunk
                    if chunk.usage:
                        prompt_tok = _usage_int(chunk.usage, "prompt_tokens")
                        completion_tok = _usage_int(chunk.usage, "completion_tokens")
                        cache_read_tok, cache_miss_tok = _extract_cache_usage(chunk.usage)

                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    finish_reason = getattr(choice, "finish_reason", None)
                    if finish_reason:
                        stop_reason = str(finish_reason)
                    delta = choice.delta
                    if not completion_started and (delta.content or delta.tool_calls):
                        generation.update(completion_start_time=datetime.now(timezone.utc))
                        completion_started = True

                    # accumulate text
                    if delta.content:
                        content_parts.append(delta.content)
                        if on_token:
                            on_token(delta.content)

                    # accumulate tool calls across chunks
                    if delta.tool_calls:
                        for tc_delta in delta.tool_calls:
                            idx = tc_delta.index
                            if idx not in tc_map:
                                tc_map[idx] = {"id": "", "name": "", "args": ""}
                            if tc_delta.id:
                                tc_map[idx]["id"] = tc_delta.id
                            if tc_delta.function:
                                if tc_delta.function.name:
                                    tc_map[idx]["name"] = tc_delta.function.name
                                if tc_delta.function.arguments:
                                    tc_map[idx]["args"] += tc_delta.function.arguments

                # parse accumulated tool calls
                parsed: list[ToolCall] = []
                for idx in sorted(tc_map):
                    raw = tc_map[idx]
                    args, parse_error = _parse_tool_arguments(raw.get("args", ""))
                    parsed.append(
                        ToolCall(
                            id=raw["id"],
                            name=raw["name"],
                            arguments=args,
                            raw_arguments=raw.get("args", ""),
                            parse_error=parse_error,
                        )
                    )

                self.total_prompt_tokens += prompt_tok
                self.total_completion_tokens += completion_tok
                self.total_cache_read_tokens += cache_read_tok
                self.total_cache_miss_tokens += cache_miss_tok

                response = LLMResponse(
                    content="".join(content_parts),
                    tool_calls=parsed,
                    prompt_tokens=prompt_tok,
                    completion_tokens=completion_tok,
                    cache_read_tokens=cache_read_tok,
                    cache_miss_tokens=cache_miss_tok,
                    stop_reason=stop_reason,
                )
            except Exception as exc:
                generation.update(
                    output={"error": str(exc)},
                    metadata={
                        "backend": type(self).__name__,
                        "error_type": type(exc).__name__,
                    },
                    level="ERROR",
                    status_message=str(exc),
                )
                raise

            generation.update(
                output={
                    "content": response.content,
                    "tool_calls": [_tool_call_payload(tool_call) for tool_call in response.tool_calls],
                },
                model=self.model,
                model_parameters=self.extra or None,
                usage_details=_langfuse_usage(response),
                metadata={
                    "backend": type(self).__name__,
                    "tool_call_count": len(response.tool_calls),
                    "stop_reason": response.stop_reason,
                },
            )
            return response

    def _call_with_retry(self, params: dict, max_retries: int = 3):
        """Retry on transient errors with exponential backoff."""
        for attempt in range(max_retries):
            try:
                return self.client.chat.completions.create(**params)
            except (RateLimitError, APITimeoutError, APIConnectionError):
                if attempt == max_retries - 1:
                    raise
                time.sleep(2 ** attempt)
            except APIError as exc:
                # 5xx = server error, retry; 4xx = client error, don't
                if exc.status_code and exc.status_code >= 500 and attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    raise


class AnthropicMessagesLLM(LLM):
    """Native Anthropic Messages backend using the official Python SDK."""

    api_format = "messages"
    supports_streaming_tool_calls = True

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        langfuse_public_key: str = "",
        langfuse_secret_key: str = "",
        langfuse_base_url: str | None = None,
        tracer: LangfuseTracer | None = None,
        **kwargs,
    ):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.langfuse_public_key = langfuse_public_key
        self.langfuse_secret_key = langfuse_secret_key
        self.langfuse_base_url = langfuse_base_url
        self.client = Anthropic(
            api_key=api_key,
            base_url=base_url,
            max_retries=0,
        )
        self.extra = kwargs
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_cache_read_tokens = 0
        self.total_cache_miss_tokens = 0
        self.tracer = tracer or LangfuseTracer(
            public_key=langfuse_public_key,
            secret_key=langfuse_secret_key,
            base_url=langfuse_base_url,
        )

    def clone(self) -> "AnthropicMessagesLLM":
        return type(self)(
            model=self.model,
            api_key=self.api_key,
            base_url=self.base_url,
            langfuse_public_key=self.langfuse_public_key,
            langfuse_secret_key=self.langfuse_secret_key,
            langfuse_base_url=self.langfuse_base_url,
            tracer=self.tracer,
            **self.extra,
        )

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_token=None,
        on_tool_call=None,
    ) -> LLMResponse:
        """Send one streaming Messages request and normalize its response."""
        system, message_params = self._split_system(messages)
        request_tools = self._convert_tools(tools or [])
        max_tokens = int(self.extra.get("max_tokens", DEFAULT_MAX_OUTPUT_TOKENS))
        params: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": message_params,
        }
        if system:
            params["system"] = system
        if request_tools:
            params["tools"] = request_tools
        for name in ("temperature", "top_p", "top_k", "stop_sequences"):
            value = self.extra.get(name)
            if value is not None:
                params[name] = value

        trace_input = {
            "system": system,
            "messages": message_params,
            "tools": request_tools,
        }
        model_parameters = {
            key: value
            for key, value in params.items()
            if key not in {"messages", "system", "tools"}
        }
        with self.tracer.start_generation(
            name="llm.chat",
            input_payload=trace_input,
            model=self.model,
            model_parameters=model_parameters,
            metadata={
                "backend": type(self).__name__,
                "api_format": self.api_format,
                "tool_schema_count": len(request_tools),
            },
        ) as generation:
            try:
                final_message, first_content_at = self._call_with_retry(
                    params,
                    on_token=on_token,
                    on_tool_call=on_tool_call,
                )
                if first_content_at is not None:
                    generation.update(completion_start_time=first_content_at)
                response = self._normalize_response(final_message)
            except Exception as exc:
                generation.update(
                    output={"error": str(exc)},
                    metadata={
                        "backend": type(self).__name__,
                        "api_format": self.api_format,
                        "error_type": type(exc).__name__,
                    },
                    level="ERROR",
                    status_message=str(exc),
                )
                raise

            self.total_prompt_tokens += response.prompt_tokens
            self.total_completion_tokens += response.completion_tokens
            self.total_cache_read_tokens += response.cache_read_tokens
            self.total_cache_miss_tokens += response.cache_miss_tokens
            generation.update(
                output={
                    "content": response.content,
                    "tool_calls": [_tool_call_payload(tool_call) for tool_call in response.tool_calls],
                },
                model=self.model,
                model_parameters=model_parameters,
                usage_details=_langfuse_usage(response),
                metadata={
                    "backend": type(self).__name__,
                    "api_format": self.api_format,
                    "tool_call_count": len(response.tool_calls),
                    "stop_reason": response.stop_reason,
                },
            )
            return response

    def _call_with_retry(
        self,
        params: dict,
        on_token=None,
        on_tool_call=None,
        max_retries: int = 3,
    ):
        for attempt in range(max_retries + 1):
            emitted_output = False
            try:
                first_content_at = None
                with self.client.messages.stream(**params) as stream:
                    for event in stream:
                        event_type = _field(event, "type")
                        if event_type == "text":
                            text = str(_field(event, "text") or "")
                            if not text:
                                continue
                            emitted_output = True
                            if first_content_at is None:
                                first_content_at = datetime.now(timezone.utc)
                            if on_token:
                                on_token(text)
                            continue
                        if event_type != "content_block_stop":
                            continue
                        block = _field(event, "content_block")
                        if _field(block, "type") != "tool_use":
                            continue
                        arguments = _field(block, "input") or {}
                        if not isinstance(arguments, dict):
                            arguments = {}
                        emitted_output = True
                        if first_content_at is None:
                            first_content_at = datetime.now(timezone.utc)
                        if on_tool_call:
                            on_tool_call(
                                ToolCall(
                                    id=str(_field(block, "id") or ""),
                                    name=str(_field(block, "name") or ""),
                                    arguments=dict(arguments),
                                    raw_arguments=json.dumps(arguments, ensure_ascii=False),
                                )
                            )
                    final_message = stream.get_final_message()
                if first_content_at is None and _field(final_message, "content"):
                    first_content_at = datetime.now(timezone.utc)
                return final_message, first_content_at
            except (
                AnthropicRateLimitError,
                AnthropicAPITimeoutError,
                AnthropicAPIConnectionError,
            ):
                # Retrying after user-visible text was emitted would duplicate
                # the prefix in CLI/Web streaming output.
                if emitted_output or attempt == max_retries:
                    raise
                time.sleep((2**attempt) * (1 - 0.25 * random()))
            except AnthropicAPIStatusError as exc:
                if (
                    exc.status_code >= 500
                    and not emitted_output
                    and attempt < max_retries
                ):
                    time.sleep((2**attempt) * (1 - 0.25 * random()))
                    continue
                raise


    @staticmethod
    def _split_system(messages: list[dict]) -> tuple[str, list[dict]]:
        system_parts: list[str] = []
        message_params: list[dict] = []
        for message in messages:
            if message.get("role") == "system":
                content = message.get("content")
                if content:
                    system_parts.append(str(content))
                continue
            message_params.append(message)
        return "\n\n".join(system_parts), message_params

    @staticmethod
    def _convert_tools(tools: list[dict]) -> list[dict]:
        converted: list[dict] = []
        for tool in tools:
            if tool.get("type") != "function":
                continue
            function = tool.get("function") or {}
            converted.append(
                {
                    "name": str(function.get("name") or ""),
                    "description": str(function.get("description") or ""),
                    "input_schema": function.get("parameters")
                    or {"type": "object", "properties": {}},
                }
            )
        return converted

    @staticmethod
    def _normalize_response(message) -> LLMResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in _field(message, "content") or []:
            block_type = _field(block, "type")
            if block_type == "text":
                text_parts.append(str(_field(block, "text") or ""))
            elif block_type == "tool_use":
                arguments = _field(block, "input") or {}
                if not isinstance(arguments, dict):
                    arguments = {}
                tool_calls.append(
                    ToolCall(
                        id=str(_field(block, "id") or ""),
                        name=str(_field(block, "name") or ""),
                        arguments=dict(arguments),
                        raw_arguments=json.dumps(arguments, ensure_ascii=False),
                    )
                )

        usage = _field(message, "usage")
        input_tokens = _usage_int(usage, "input_tokens")
        output_tokens = _usage_int(usage, "output_tokens")
        cache_read = _usage_int(usage, "cache_read_input_tokens")
        cache_creation = _usage_int(usage, "cache_creation_input_tokens")
        return LLMResponse(
            content="".join(text_parts),
            tool_calls=tool_calls,
            prompt_tokens=input_tokens + cache_read + cache_creation,
            completion_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_miss_tokens=input_tokens + cache_creation,
            stop_reason=str(_field(message, "stop_reason") or ""),
        )


def is_retryable_llm_error(exc: Exception) -> bool:
    """Return whether a fresh model-step transaction may safely retry the request."""
    if isinstance(
        exc,
        (
            RateLimitError,
            APITimeoutError,
            APIConnectionError,
            AnthropicRateLimitError,
            AnthropicAPITimeoutError,
            AnthropicAPIConnectionError,
        ),
    ):
        return True
    if isinstance(exc, (APIError, AnthropicAPIStatusError)):
        status_code = getattr(exc, "status_code", None)
        return bool(status_code and status_code >= 500)
    return False


class LiteLLM(LLM):
    """LLM backend via LiteLLM, supporting 100+ providers.

    Use this when your target provider is NOT OpenAI-compatible
    (AWS Bedrock, Google Vertex, Cohere, etc.) or when you want
    a single interface to switch between any provider by changing
    the model string.

    Set AUTOCODE_PROVIDER=litellm and use LiteLLM model strings
    like ``anthropic/claude-3-haiku``, ``bedrock/anthropic.claude-v2``,
    ``vertex_ai/gemini-pro``, etc.
    """

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        langfuse_public_key: str = "",
        langfuse_secret_key: str = "",
        langfuse_base_url: str | None = None,
        tracer: LangfuseTracer | None = None,
        **kwargs,
    ):
        # skip LLM.__init__ which creates an OpenAI client
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.langfuse_public_key = langfuse_public_key
        self.langfuse_secret_key = langfuse_secret_key
        self.langfuse_base_url = langfuse_base_url
        self.extra = kwargs
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_cache_read_tokens = 0
        self.total_cache_miss_tokens = 0
        self.tracer = tracer or LangfuseTracer(
            public_key=langfuse_public_key,
            secret_key=langfuse_secret_key,
            base_url=langfuse_base_url,
        )

    def clone(self) -> "LiteLLM":
        return type(self)(
            model=self.model,
            api_key=self.api_key,
            base_url=self.base_url,
            langfuse_public_key=self.langfuse_public_key,
            langfuse_secret_key=self.langfuse_secret_key,
            langfuse_base_url=self.langfuse_base_url,
            tracer=self.tracer,
            **self.extra,
        )

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_token=None,
    ) -> LLMResponse:
        """Send messages via litellm, stream back response, handle tool calls."""
        params: dict = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            **self.extra,
        }
        if tools:
            params["tools"] = tools

        with self.tracer.start_generation(
            name="llm.chat",
            input_payload={"messages": messages, "tools": tools or []},
            model=self.model,
            model_parameters=self.extra,
            metadata={
                "backend": type(self).__name__,
                "tool_schema_count": len(tools or []),
            },
        ) as generation:
            try:
                stream = self._call_with_retry(params)

                content_parts: list[str] = []
                tc_map: dict[int, dict] = {}
                prompt_tok = 0
                completion_tok = 0
                cache_read_tok = 0
                cache_miss_tok = 0
                completion_started = False
                stop_reason = ""

                for chunk in stream:
                    usage = getattr(chunk, "usage", None)
                    if usage:
                        prompt_tok = _usage_int(usage, "prompt_tokens")
                        completion_tok = _usage_int(usage, "completion_tokens")
                        cache_read_tok, cache_miss_tok = _extract_cache_usage(usage)

                    if not getattr(chunk, "choices", None):
                        continue
                    choice = chunk.choices[0]
                    finish_reason = getattr(choice, "finish_reason", None)
                    if finish_reason:
                        stop_reason = str(finish_reason)
                    delta = choice.delta
                    if not completion_started and (
                        getattr(delta, "content", None) or getattr(delta, "tool_calls", None)
                    ):
                        generation.update(completion_start_time=datetime.now(timezone.utc))
                        completion_started = True

                    if getattr(delta, "content", None):
                        content_parts.append(delta.content)
                        if on_token:
                            on_token(delta.content)

                    if getattr(delta, "tool_calls", None):
                        for tc_delta in delta.tool_calls:
                            idx = tc_delta.index
                            if idx not in tc_map:
                                tc_map[idx] = {"id": "", "name": "", "args": ""}
                            if tc_delta.id:
                                tc_map[idx]["id"] = tc_delta.id
                            if tc_delta.function:
                                if tc_delta.function.name:
                                    tc_map[idx]["name"] = tc_delta.function.name
                                if tc_delta.function.arguments:
                                    tc_map[idx]["args"] += tc_delta.function.arguments

                parsed: list[ToolCall] = []
                for idx in sorted(tc_map):
                    raw = tc_map[idx]
                    args, parse_error = _parse_tool_arguments(raw.get("args", ""))
                    parsed.append(
                        ToolCall(
                            id=raw["id"],
                            name=raw["name"],
                            arguments=args,
                            raw_arguments=raw.get("args", ""),
                            parse_error=parse_error,
                        )
                    )

                self.total_prompt_tokens += prompt_tok
                self.total_completion_tokens += completion_tok
                self.total_cache_read_tokens += cache_read_tok
                self.total_cache_miss_tokens += cache_miss_tok

                response = LLMResponse(
                    content="".join(content_parts),
                    tool_calls=parsed,
                    prompt_tokens=prompt_tok,
                    completion_tokens=completion_tok,
                    cache_read_tokens=cache_read_tok,
                    cache_miss_tokens=cache_miss_tok,
                    stop_reason=stop_reason,
                )
            except Exception as exc:
                generation.update(
                    output={"error": str(exc)},
                    metadata={
                        "backend": type(self).__name__,
                        "error_type": type(exc).__name__,
                    },
                    level="ERROR",
                    status_message=str(exc),
                )
                raise

            generation.update(
                output={
                    "content": response.content,
                    "tool_calls": [_tool_call_payload(tool_call) for tool_call in response.tool_calls],
                },
                model=self.model,
                model_parameters=self.extra or None,
                usage_details=_langfuse_usage(response),
                metadata={
                    "backend": type(self).__name__,
                    "tool_call_count": len(response.tool_calls),
                    "stop_reason": response.stop_reason,
                },
            )
            return response

    def _call_with_retry(self, params: dict, max_retries: int = 3):
        """Retry on transient errors with exponential backoff via litellm."""
        import litellm

        params["drop_params"] = True
        if self.api_key:
            params["api_key"] = self.api_key
        if self.base_url:
            params["api_base"] = self.base_url

        for attempt in range(max_retries):
            try:
                return litellm.completion(**params)
            except Exception as e:
                err = str(e).lower()
                is_transient = any(
                    kw in err
                    for kw in ["rate_limit", "timeout", "connection", "502", "503", "529"]
                )
                is_server = any(kw in err for kw in ["500", "502", "503", "504"])
                if (is_transient or is_server) and attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    raise


def llm_class_for_provider(provider: str):
    """Resolve the configured provider without silently changing protocols."""
    normalized = provider.strip().lower()
    providers = {
        "anthropic": AnthropicMessagesLLM,
        "openai": LLM,
        "litellm": LiteLLM,
    }
    try:
        return providers[normalized]
    except KeyError as exc:
        supported = ", ".join(sorted(providers))
        raise ValueError(
            f"Unsupported AUTOCODE_PROVIDER '{provider}'. Expected one of: {supported}."
        ) from exc


def api_format_for_provider(provider: str) -> str:
    return str(llm_class_for_provider(provider).api_format)


def _tool_call_payload(tool_call: ToolCall) -> dict:
    payload = {
        "id": tool_call.id,
        "name": tool_call.name,
        "arguments": dict(tool_call.arguments),
    }
    if tool_call.raw_arguments is not None:
        payload["raw_arguments"] = tool_call.raw_arguments
    if tool_call.parse_error is not None:
        payload["parse_error"] = tool_call.parse_error
    return payload

