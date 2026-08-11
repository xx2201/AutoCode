import binascii
from types import SimpleNamespace

import httpx
import pytest
from anthropic import APIConnectionError, APIStatusError

from autocode.agent import Agent
from autocode.llm import AnthropicMessagesLLM, LLM, LiteLLM, llm_class_for_provider
from autocode.message_projection import serialize_anthropic_messages


def _usage(**overrides):
    values = {
        "input_tokens": 10,
        "output_tokens": 4,
        "cache_read_input_tokens": 3,
        "cache_creation_input_tokens": 2,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _message(content, *, usage=None, stop_reason="end_turn"):
    return SimpleNamespace(
        content=content,
        usage=usage or _usage(),
        stop_reason=stop_reason,
    )


def test_anthropic_projection_keeps_image_inside_its_tool_result():
    history = [
        {
            "role": "user",
            "content": "inspect upload",
            "model_content": [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64,YWJj"},
                }
            ],
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "read-image",
                    "type": "function",
                    "function": {
                        "name": "read",
                        "arguments": '{"file_path":"diagram.png"}',
                    },
                },
                {
                    "id": "read-text",
                    "type": "function",
                    "function": {
                        "name": "read",
                        "arguments": '{"file_path":"notes.txt"}',
                    },
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "read-image",
            "tool_name": "read",
            "content": "Loaded image",
            "model_content": [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,YWJj"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "read-text",
            "tool_name": "read",
            "content": "hello",
        },
    ]

    projected = serialize_anthropic_messages("system", history)

    assert projected[0] == {"role": "system", "content": "system"}
    assert projected[1]["role"] == "user"
    assert projected[1]["content"][1] == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": "YWJj",
        },
    }
    assert [block["type"] for block in projected[2]["content"]] == [
        "tool_use",
        "tool_use",
    ]
    tool_result_message = projected[3]
    assert tool_result_message["role"] == "user"
    assert [block["type"] for block in tool_result_message["content"]] == [
        "tool_result",
        "tool_result",
    ]
    image_result = tool_result_message["content"][0]
    assert image_result["tool_use_id"] == "read-image"
    assert image_result["content"][1]["type"] == "image"
    assert image_result["content"][1]["source"]["media_type"] == "image/png"
    assert "Visual content returned by tools" not in str(projected)


def test_anthropic_projection_merges_consecutive_user_messages():
    projected = serialize_anthropic_messages(
        "system",
        [
            {"role": "user", "content": "first"},
            {"role": "user", "content": "steer"},
        ],
    )

    assert projected[1] == {
        "role": "user",
        "content": [
            {"type": "text", "text": "first"},
            {"type": "text", "text": "steer"},
        ],
    }


def test_anthropic_projection_uses_valid_empty_tool_result():
    projected = serialize_anthropic_messages(
        "system",
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "empty-call",
                        "type": "function",
                        "function": {"name": "noop", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "empty-call",
                "content": "",
            },
        ],
    )

    assert projected[2]["content"][0]["content"] == ""


def test_anthropic_projection_rejects_malformed_image_data():
    with pytest.raises(binascii.Error):
        serialize_anthropic_messages(
            "system",
            [
                {
                    "role": "user",
                    "content": "look",
                    "model_content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,not-valid!"},
                        }
                    ],
                }
            ],
        )


def test_agent_selects_messages_projection_for_anthropic_backend(tmp_path):
    llm = AnthropicMessagesLLM(
        model="macaron-v1-coding-venti",
        api_key="sk-test",
        base_url="https://example.com",
    )
    agent = Agent(llm=llm, tools=[], workspace_root=str(tmp_path))
    agent.messages = [{"role": "user", "content": "hello"}]

    request = agent._request_messages()

    assert request[0]["role"] == "system"
    assert request[1]["role"] == "user"
    assert request[1]["content"][0] == {"type": "text", "text": "hello"}


def test_anthropic_backend_streams_text_and_normalizes_tool_calls():
    final = _message(
        [
            SimpleNamespace(type="text", text="done"),
            SimpleNamespace(
                type="tool_use",
                id="call-1",
                name="read",
                input={"file_path": "README.md"},
            ),
        ],
        stop_reason="tool_use",
    )
    captured = {}

    class _Stream:
        def __iter__(self):
            yield SimpleNamespace(type="text", text="do")
            yield SimpleNamespace(type="text", text="ne")
            yield SimpleNamespace(
                type="content_block_stop",
                content_block=final.content[1],
            )

        def get_final_message(self):
            return final

    class _Manager:
        def __enter__(self):
            return _Stream()

        def __exit__(self, exc_type, exc, tb):
            return False

    class _Messages:
        def stream(self, **params):
            captured.update(params)
            return _Manager()

    llm = AnthropicMessagesLLM(
        model="macaron-v1-coding-venti",
        api_key="sk-test",
        base_url="https://example.com",
        max_tokens=256,
        temperature=0,
    )
    llm.client = SimpleNamespace(messages=_Messages())
    streamed = []
    streamed_tools = []

    response = llm.chat(
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": [{"type": "text", "text": "go"}]},
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "read",
                    "description": "Read a file",
                    "parameters": {
                        "type": "object",
                        "properties": {"file_path": {"type": "string"}},
                    },
                },
            }
        ],
        on_token=streamed.append,
        on_tool_call=streamed_tools.append,
    )

    assert streamed == ["do", "ne"]
    assert streamed_tools[0].id == "call-1"
    assert streamed_tools[0].arguments == {"file_path": "README.md"}
    assert captured["system"] == "system"
    assert captured["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "go"}]}
    ]
    assert captured["tools"][0]["input_schema"]["properties"]["file_path"] == {
        "type": "string"
    }
    assert captured["output_config"] == {"effort": "high"}
    assert response.content == "done"
    assert response.tool_calls[0].id == "call-1"
    assert response.tool_calls[0].arguments == {"file_path": "README.md"}
    assert response.prompt_tokens == 15
    assert response.stop_reason == "tool_use"
    assert response.completion_tokens == 4
    assert response.cache_read_tokens == 3
    assert response.cache_miss_tokens == 12


def test_anthropic_backend_preserves_thinking_blocks_for_the_next_turn():
    final = _message(
        [
            {
                "type": "thinking",
                "thinking": "inspect the repository",
                "signature": "signed-thinking",
            },
            {"type": "text", "text": "done"},
        ]
    )

    response = AnthropicMessagesLLM._normalize_response(final)
    projected = serialize_anthropic_messages(
        "system",
        [
            {"role": "user", "content": "start"},
            response.message,
            {"role": "user", "content": "continue"},
        ],
    )

    assert response.content == "done"
    assert [block["type"] for block in response.model_content] == [
        "thinking",
        "text",
    ]
    assert projected[2]["content"] == response.model_content


def test_anthropic_backend_does_not_retry_after_streaming_visible_text():
    calls = 0

    class _BrokenStream:
        def __iter__(self):
            yield SimpleNamespace(type="text", text="partial")
            raise APIConnectionError(request=httpx.Request("POST", "https://example.com"))

    class _Manager:
        def __enter__(self):
            return _BrokenStream()

        def __exit__(self, exc_type, exc, tb):
            return False

    class _Messages:
        def stream(self, **params):
            nonlocal calls
            calls += 1
            return _Manager()

    llm = AnthropicMessagesLLM(
        model="macaron-v1-coding-venti",
        api_key="sk-test",
        base_url="https://example.com",
    )
    llm.client = SimpleNamespace(messages=_Messages())
    streamed = []

    with pytest.raises(APIConnectionError):
        llm._call_with_retry(
            {"model": llm.model, "max_tokens": 16, "messages": []},
            on_token=streamed.append,
        )

    assert streamed == ["partial"]
    assert calls == 1


def test_anthropic_backend_does_not_retry_after_streaming_thinking():
    calls = 0

    class _BrokenStream:
        def __iter__(self):
            yield SimpleNamespace(type="thinking", thinking="partial reasoning")
            raise APIConnectionError(request=httpx.Request("POST", "https://example.com"))

    class _Manager:
        def __enter__(self):
            return _BrokenStream()

        def __exit__(self, exc_type, exc, tb):
            return False

    class _Messages:
        def stream(self, **params):
            nonlocal calls
            calls += 1
            return _Manager()

    llm = AnthropicMessagesLLM(
        model="macaron-v1-coding-venti",
        api_key="sk-test",
        base_url="https://example.com",
    )
    llm.client = SimpleNamespace(messages=_Messages())

    with pytest.raises(APIConnectionError):
        llm._call_with_retry(
            {"model": llm.model, "max_tokens": 16, "messages": []},
        )

    assert calls == 1


@pytest.mark.parametrize("status_code", [502, 503, 529])
def test_anthropic_backend_retries_transient_server_errors_with_backoff(
    monkeypatch,
    status_code,
):
    calls = 0

    class _Stream:
        def __iter__(self):
            return iter(())

        def get_final_message(self):
            return _message([])

    class _Manager:
        def __enter__(self):
            return _Stream()

        def __exit__(self, exc_type, exc, tb):
            return False

    class _Messages:
        def stream(self, **params):
            nonlocal calls
            calls += 1
            if calls <= 3:
                request = httpx.Request("POST", "https://example.com/v1/messages")
                response = httpx.Response(status_code, request=request)
                raise APIStatusError("upstream failed", response=response, body=None)
            return _Manager()

    llm = AnthropicMessagesLLM(
        model="macaron-v1-coding-venti",
        api_key="sk-test",
        base_url="https://example.com",
    )
    llm.client = SimpleNamespace(messages=_Messages())
    sleeps = []
    monkeypatch.setattr("autocode.llm.random", lambda: 0)
    monkeypatch.setattr("autocode.llm.time.sleep", sleeps.append)

    message, first_content_at = llm._call_with_retry(
        {"model": llm.model, "max_tokens": 16, "messages": []}
    )

    assert message.content == []
    assert first_content_at is None
    assert calls == 4
    assert sleeps == [1, 2, 4]


def test_provider_factory_keeps_all_three_backends():
    assert llm_class_for_provider("anthropic") is AnthropicMessagesLLM
    assert llm_class_for_provider("openai") is LLM
    assert llm_class_for_provider("litellm") is LiteLLM


def test_provider_factory_rejects_unknown_protocol():
    try:
        llm_class_for_provider("unknown")
    except ValueError as exc:
        assert "Expected one of: anthropic, litellm, openai" in str(exc)
    else:
        raise AssertionError("unknown provider must be rejected")
