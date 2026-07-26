import base64
import types

from PIL import Image

from autocode.agent import Agent
from autocode.attachments import prepare_attachments
from autocode.llm import LLM, LLMResponse, ToolCall
from autocode.state import SessionState
from autocode.tools.read import ReadTool


def _content_chunk(content: str):
    delta = types.SimpleNamespace(content=content, tool_calls=None)
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(delta=delta)],
        usage=None,
    )


def _tool_chunk(tool_id: str, name: str, arguments: str):
    function = types.SimpleNamespace(name=name, arguments=arguments)
    tool_call = types.SimpleNamespace(index=0, id=tool_id, function=function)
    delta = types.SimpleNamespace(content=None, tool_calls=[tool_call])
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(delta=delta)],
        usage=None,
    )


def _usage_chunk():
    usage = types.SimpleNamespace(prompt_tokens=8, completion_tokens=2)
    return types.SimpleNamespace(choices=[], usage=usage)


def test_uploaded_image_reaches_the_model_and_workspace(tmp_path):
    image_bytes = b"\x89PNG\r\n\x1a\nmultimodal-test"
    encoded = base64.b64encode(image_bytes).decode("ascii")

    prepared = prepare_attachments(
        tmp_path,
        "web_12345678",
        "这张图里有什么？",
        [
            {
                "name": "screen.png",
                "media_type": "image/png",
                "data_base64": encoded,
            }
        ],
    )

    assert prepared.files[0]["path"].startswith(".autocode/uploads/")
    assert (tmp_path / prepared.files[0]["path"]).read_bytes() == image_bytes
    assert prepared.image_parts[0]["image_url"]["url"] == f"data:image/png;base64,{encoded}"

    captured = []
    llm = LLM(model="vision-model", api_key="sk-test")

    def call(params):
        captured.append(params)
        return iter([_content_chunk("我看到了图片"), _usage_chunk()])

    llm._call_with_retry = call
    agent = Agent(llm=llm, tools=[], workspace_root=str(tmp_path))
    agent.memory.schedule_project_memory_refresh = lambda *args, **kwargs: None

    reply = agent.chat(prepared.prompt, image_parts=prepared.image_parts)

    assert reply == "我看到了图片"
    user_message = next(
        item
        for item in captured[0]["messages"]
        if item.get("role") == "user" and isinstance(item.get("content"), list)
    )
    assert user_message["content"][0]["type"] == "text"
    assert user_message["content"][1]["type"] == "image_url"
    assert agent.messages[0]["content"] == prepared.prompt
    assert not any(isinstance(message.get("content"), list) for message in agent.messages)
    assert captured[0]["messages"][-1] is user_message


def test_read_tool_feeds_workspace_image_into_next_model_round(tmp_path):
    image_path = tmp_path / "diagram.png"
    Image.new("RGB", (4, 4), "red").save(image_path)
    captured = []
    llm = LLM(model="vision-model", api_key="sk-test")

    def call(params):
        captured.append(params)
        if len(captured) == 1:
            return iter(
                [
                    _tool_chunk("call_image", "read", '{"file_path":"diagram.png"}'),
                    _usage_chunk(),
                ]
            )
        return iter([_content_chunk("图片已读取"), _usage_chunk()])

    llm._call_with_retry = call
    agent = Agent(llm=llm, workspace_root=str(tmp_path))
    agent.memory.schedule_project_memory_refresh = lambda *args, **kwargs: None

    reply = agent.chat("读取 diagram.png 并描述")

    assert reply == "图片已读取"
    second_round = captured[1]["messages"]
    visual_message = next(
        item
        for item in second_round
        if item.get("role") == "user" and isinstance(item.get("content"), list)
    )
    assert visual_message["content"][1]["type"] == "image_url"
    assert visual_message["content"][1]["image_url"]["url"].startswith(
        "data:image/jpeg;base64,"
    )
    assert not any(isinstance(message.get("content"), list) for message in agent.messages)


def test_all_parallel_tool_results_precede_visual_user_message(tmp_path):
    Image.new("RGB", (4, 4), "red").save(tmp_path / "diagram.png")
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")

    class RecordingLLM:
        model = "vision-model"
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_cache_read_tokens = 0
        total_cache_miss_tokens = 0

        def __init__(self):
            self.calls = []

        def chat(self, messages, tools=None, on_token=None):
            self.calls.append(messages)
            if len(self.calls) == 1:
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="image",
                            name="read",
                            arguments={"file_path": "diagram.png"},
                        ),
                        ToolCall(
                            id="text",
                            name="read",
                            arguments={"file_path": "note.txt"},
                        ),
                    ],
                )
            return LLMResponse(content="done")

    llm = RecordingLLM()
    agent = Agent(
        llm=llm,
        tools=[ReadTool()],
        workspace_root=str(tmp_path),
    )
    agent.memory.schedule_project_memory_refresh = lambda *args, **kwargs: None

    assert agent.chat("inspect both") == "done"

    second_round = llm.calls[1]
    image_tool_index = next(
        index
        for index, message in enumerate(second_round)
        if message.get("role") == "tool" and message.get("tool_call_id") == "image"
    )
    text_tool_index = next(
        index
        for index, message in enumerate(second_round)
        if message.get("role") == "tool" and message.get("tool_call_id") == "text"
    )
    visual_user_index = max(
        index
        for index, message in enumerate(second_round)
        if message.get("role") == "user"
        and isinstance(message.get("content"), list)
    )
    assert image_tool_index < visual_user_index
    assert text_tool_index < visual_user_index


def test_repeated_read_keeps_one_ephemeral_image_for_current_turn(tmp_path):
    Image.new("RGB", (4, 4), "red").save(tmp_path / "diagram.png")

    class RepeatingReadLLM:
        model = "vision-model"
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_cache_read_tokens = 0
        total_cache_miss_tokens = 0

        def __init__(self):
            self.calls = []

        def chat(self, messages, tools=None, on_token=None):
            self.calls.append(messages)
            if len(self.calls) < 3:
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id=f"image-{len(self.calls)}",
                            name="read",
                            arguments={"file_path": "diagram.png", "detail": "high"},
                        )
                    ],
                )
            return LLMResponse(content="done")

    llm = RepeatingReadLLM()
    agent = Agent(llm=llm, tools=[ReadTool()], workspace_root=str(tmp_path))
    agent.memory.schedule_project_memory_refresh = lambda *args, **kwargs: None

    assert agent.chat("inspect image") == "done"
    final_visual = [
        message
        for message in llm.calls[-1]
        if isinstance(message.get("content"), list)
    ]
    assert len(final_visual) == 1
    assert len(final_visual[0]["content"]) == 2
    assert not any(isinstance(message.get("content"), list) for message in agent.messages)


def test_restore_removes_legacy_tool_visual_carriers(tmp_path):
    agent = Agent(
        llm=LLM(model="vision-model", api_key="sk-test"),
        tools=[],
        workspace_root=str(tmp_path),
    )
    messages = [
        {"role": "user", "content": "real question"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Visual content loaded by tools: read."},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64,abc"},
                },
            ],
        },
        {"role": "assistant", "content": "answer"},
    ]

    agent.restore_session(SessionState(session_id="session_visual"), messages)

    assert [message["content"] for message in agent.messages] == ["real question", "answer"]
