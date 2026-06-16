import types as builtin_types

import pytest

from autocode.llm import LLM, LiteLLM, LLMResponse


def _tool_chunk(*, index: int, call_id: str | None = None, name: str | None = None, arguments: str | None = None):
    function = builtin_types.SimpleNamespace(name=name, arguments=arguments)
    tool_call = builtin_types.SimpleNamespace(index=index, id=call_id, function=function)
    delta = builtin_types.SimpleNamespace(content=None, tool_calls=[tool_call])
    choice = builtin_types.SimpleNamespace(delta=delta)
    return builtin_types.SimpleNamespace(choices=[choice], usage=None)


def _usage_chunk(prompt: int = 10, completion: int = 5):
    usage = builtin_types.SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion)
    return builtin_types.SimpleNamespace(choices=[], usage=usage)


def _usage_chunk_with_cache(
    *,
    prompt: int = 10,
    completion: int = 5,
    cache_hit: int = 6,
    cache_miss: int = 4,
):
    usage = builtin_types.SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        prompt_cache_hit_tokens=cache_hit,
        prompt_cache_miss_tokens=cache_miss,
    )
    return builtin_types.SimpleNamespace(choices=[], usage=usage)


@pytest.mark.parametrize(
    ("label", "factory"),
    [
        ("openai", lambda: LLM(model="fake-model", api_key="sk-test")),
        ("litellm", lambda: LiteLLM(model="fake-model")),
    ],
)
def test_chat_preserves_invalid_tool_call_arguments(label, factory):
    llm = factory()
    raw_arguments = '{"file_path":"demo.txt","content":"hello"'
    stream = iter(
        [
            _tool_chunk(index=0, call_id="call_1", name="write_file", arguments='{"file_path":"demo.txt",'),
            _tool_chunk(index=0, arguments='"content":"hello"'),
            _usage_chunk(prompt=12, completion=3),
        ]
    )
    llm._call_with_retry = lambda params: stream

    result = llm.chat(messages=[{"role": "user", "content": "write file"}], tools=[{"type": "function"}])

    assert isinstance(result, LLMResponse)
    assert len(result.tool_calls) == 1
    tool_call = result.tool_calls[0]
    assert tool_call.name == "write_file"
    assert tool_call.arguments == {}
    assert tool_call.raw_arguments == raw_arguments
    assert tool_call.parse_error is not None
    assert "not valid JSON" in tool_call.parse_error
    assert result.message["tool_calls"][0]["function"]["arguments"] == raw_arguments


@pytest.mark.parametrize(
    ("label", "factory"),
    [
        ("openai", lambda: LLM(model="fake-model", api_key="sk-test")),
        ("litellm", lambda: LiteLLM(model="fake-model")),
    ],
)
def test_chat_tracks_prompt_cache_usage(label, factory):
    llm = factory()
    llm._call_with_retry = lambda params: iter([_usage_chunk_with_cache(prompt=14, completion=3, cache_hit=9, cache_miss=5)])

    result = llm.chat(messages=[{"role": "user", "content": "hi"}])

    assert result.prompt_tokens == 14
    assert result.completion_tokens == 3
    assert result.cache_read_tokens == 9
    assert result.cache_miss_tokens == 5
    assert llm.total_cache_read_tokens == 9
    assert llm.total_cache_miss_tokens == 5
