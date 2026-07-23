import base64
import types

from autocode.agent import Agent
from autocode.attachments import prepare_attachments
from autocode.llm import LLM, LLMResponse, ToolCall
from autocode.tools.image import ReadImageTool
from autocode.tools.read import ReadFileTool


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
        item for item in captured[0]["messages"] if item.get("role") == "user"
    )
    assert user_message["content"][0]["type"] == "text"
    assert user_message["content"][1]["type"] == "image_url"


def test_read_image_tool_feeds_workspace_image_into_next_model_round(tmp_path):
    image_path = tmp_path / "diagram.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nworkspace-image")
    captured = []
    llm = LLM(model="vision-model", api_key="sk-test")

    def call(params):
        captured.append(params)
        if len(captured) == 1:
            return iter(
                [
                    _tool_chunk("call_image", "read_image", '{"file_path":"diagram.png"}'),
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
        "data:image/png;base64,"
    )


def test_all_parallel_tool_results_precede_visual_user_message(tmp_path):
    (tmp_path / "diagram.png").write_bytes(b"\x89PNG\r\n\x1a\nworkspace-image")
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
                            name="read_image",
                            arguments={"file_path": "diagram.png"},
                        ),
                        ToolCall(
                            id="text",
                            name="read_file",
                            arguments={"file_path": "note.txt"},
                        ),
                    ],
                )
            return LLMResponse(content="done")

    llm = RecordingLLM()
    agent = Agent(
        llm=llm,
        tools=[ReadImageTool(), ReadFileTool()],
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
