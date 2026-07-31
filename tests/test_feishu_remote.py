from autocode.config import Config
from autocode.remote.feishu_bot import (
    _attachment_kind,
    _build_api_client,
    _build_file_upload_request,
    _build_image_upload_request,
    _build_patch_request,
    _build_reply_request,
    _guess_feishu_file_type,
    _resolve_workspace_attachment,
    _session_key,
)
from autocode.remote.feishu_formatting import (
    build_approval_card,
    build_file_content,
    build_image_content,
    build_live_status_card,
    build_resume_card,
    build_text_content,
    parse_text_content,
    split_text_chunks,
)
from autocode.remote.feishu_tool import FeishuSendTool
from autocode.remote.manager import RemoteTurnResult


def test_parse_text_content_reads_feishu_json():
    assert parse_text_content('{"text":"hello"}') == "hello"
    assert parse_text_content("plain text") == "plain text"


def test_build_text_content_preserves_unicode():
    assert build_text_content("hello 世界") == '{"text": "hello 世界"}'


def test_build_image_content_uses_image_key():
    assert build_image_content("img_123") == '{"image_key": "img_123"}'


def test_build_file_content_uses_file_key_and_name():
    assert build_file_content("file_123", "report.pdf") == '{"file_key": "file_123", "file_name": "report.pdf"}'


def test_build_approval_card_embeds_actions():
    result = RemoteTurnResult(
        text="waiting for approval",
        session_id="session_123",
        task_id="task_123",
        status="waiting_approval",
        pending_tool="shell_command",
        pending_reason="command is not in allowlist",
        pending_arguments={"command": "python app.py"},
        pending_requires_manual=True,
        pending_approval_scope="tool:shell_command",
        pending_approval_label="本任务允许运行此类命令",
    )
    card = build_approval_card(result, "user:ou_xxx", "ou_owner")
    actions = []
    for column in card["body"]["elements"][1]["columns"]:
        actions.append(column["elements"][0]["value"]["command"])
    assert actions == ["approve", "approve_scope", "reject"]
    assert "python app.py" in card["body"]["elements"][0]["content"]
    assert "Permissions" in card["body"]["elements"][0]["content"]
    assert "session_123" in card["body"]["elements"][0]["content"]


def test_build_live_status_card_shows_runtime_progress():
    card = build_live_status_card(
        title="Fix import",
        phase="Running Tool",
        status="running",
        session_id="session_123",
        task_id="task_123",
        step_index=2,
        llm_calls=1,
        tool_calls=1,
        prompt_tokens=120,
        completion_tokens=40,
        cache_read_tokens=90,
        cache_miss_tokens=30,
        last_prompt_tokens=120,
        last_completion_tokens=40,
        last_cache_read_tokens=90,
        last_cache_miss_tokens=30,
        compactions=1,
        cache_segments=2,
        last_tool="read_file",
        detail="Executing read_file.",
        permission_mode="ask",
    )
    content = card["body"]["elements"][0]["content"]
    assert "Running Tool" in content
    assert "read_file" in content
    assert "session_123" in content
    assert "task_123" in content
    assert "Prompt Tokens Total: `120`" in content
    assert "Prompt Cache Read Total: `90`" in content
    assert "Cache Segments: `2`" in content
    assert "Last Prompt Tokens: `120`" in content


def test_build_resume_card_embeds_resume_buttons():
    card = build_resume_card(
        tasks=[
            {"session_id": "session_1", "task_id": "task_1", "title": "Fix import", "status": "completed", "saved_at": "2026-06-08 23:00:00"},
            {"session_id": "session_2", "task_id": "task_2", "title": "Add tests", "status": "failed", "saved_at": "2026-06-08 23:10:00"},
        ],
        session_key="user:ou_xxx",
        owner_open_id="ou_owner",
        workspace_root="G:/repo/demo",
    )
    assert card["header"]["title"]["content"] == "Resume Session"
    resume_buttons = []
    for element in card["body"]["elements"]:
        if element["tag"] == "column_set":
            resume_buttons.append(element["columns"][0]["elements"][0]["value"])
    assert [item["command"] for item in resume_buttons] == ["resume", "resume"]
    assert [item["session_id"] for item in resume_buttons] == ["session_1", "session_2"]


def test_split_text_chunks_respects_feishu_limit():
    chunks = split_text_chunks("a" * 4000)
    assert len(chunks) > 1
    assert all(len(chunk) <= 3000 for chunk in chunks)


def test_session_key_uses_user_for_p2p():
    message = type("Message", (), {"chat_type": "p2p", "chat_id": "oc_chat"})()
    assert _session_key(message, "ou_user") == "user:ou_user"


def test_session_key_uses_chat_for_group():
    message = type("Message", (), {"chat_type": "group", "chat_id": "oc_chat"})()
    assert _session_key(message, "ou_user") == "chat:oc_chat"


def test_build_api_client_sets_feishu_domain():
    calls = []

    class _Builder:
        def __init__(self):
            self._client = type(
                "_BuiltClient",
                (),
                {
                    "_config": type("_Config", (), {"domain": None})(),
                    "im": type(
                        "_Im",
                        (),
                        {
                            "v1": type(
                                "_V1",
                                (),
                                {"message": type("_Message", (), {"config": type("_Config", (), {"domain": None})()})()},
                            )()
                        },
                    )(),
                },
            )()

        def app_id(self, value):
            calls.append(("app_id", value))
            return self

        def app_secret(self, value):
            calls.append(("app_secret", value))
            return self

        def domain(self, value):
            calls.append(("domain", value))
            return self

        def build(self):
            calls.append(("build", None))
            return self._client

    class _Client:
        @staticmethod
        def builder():
            return _Builder()

    client = _build_api_client(
        {"Client": _Client, "FEISHU_DOMAIN": "https://open.feishu.cn"},
        Config(feishu_app_id="cli_xxx", feishu_app_secret="secret"),
    )
    assert client._config.domain == "https://open.feishu.cn"
    assert client.im.v1.message.config.domain == "https://open.feishu.cn"
    assert ("domain", "https://open.feishu.cn") in calls


def test_build_reply_request_uses_builder_initialized_uri():
    request = _build_reply_request(_fake_lark_request_api(), "om_123", "text", '{"text":"ok"}')
    assert request.uri == "/open-apis/im/v1/messages/:message_id/reply"
    assert request.paths["message_id"] == "om_123"
    assert request.body.msg_type == "text"


def test_build_patch_request_uses_builder_initialized_uri():
    request = _build_patch_request(_fake_lark_request_api(), "om_123", '{"schema":"2.0"}')
    assert request.uri == "/open-apis/im/v1/messages/:message_id"
    assert request.paths["message_id"] == "om_123"
    assert request.body.content == '{"schema":"2.0"}'


def test_build_image_upload_request_uses_image_endpoint():
    import io

    request = _build_image_upload_request(_fake_lark_request_api(), io.BytesIO(b"image"))
    assert request.uri == "/open-apis/im/v1/images"
    assert request.body.image_type == "message"


def test_build_file_upload_request_uses_file_endpoint():
    import io

    request = _build_file_upload_request(_fake_lark_request_api(), io.BytesIO(b"pdf"), "report.pdf")
    assert request.uri == "/open-apis/im/v1/files"
    assert request.body.file_name == "report.pdf"
    assert request.body.file_type == "pdf"


def test_guess_feishu_file_type_defaults_to_stream():
    assert _guess_feishu_file_type("notes.txt") == "stream"
    assert _guess_feishu_file_type("slides.pptx") == "ppt"


def test_attachment_kind_detects_images():
    from pathlib import Path

    assert _attachment_kind(Path("shot.png")) == "image"
    assert _attachment_kind(Path("report.pdf")) == "file"


def test_resolve_workspace_attachment_accepts_relative_file(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "report.pdf"
    target.write_text("demo", encoding="utf-8")

    resolved = _resolve_workspace_attachment(str(workspace), "report.pdf")

    assert resolved == target.resolve()


def test_resolve_workspace_attachment_rejects_outside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.pdf"
    outside.write_text("demo", encoding="utf-8")

    try:
        _resolve_workspace_attachment(str(workspace), str(outside))
    except ValueError as exc:
        assert "path must stay inside workspace" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_feishu_send_tool_uses_sender_callback():
    calls = []

    tool = FeishuSendTool(lambda path: calls.append(path) or f"sent:{path}")

    assert tool.execute("reports/out.pdf") == "sent:reports/out.pdf"
    assert calls == ["reports/out.pdf"]


def test_feishu_send_tool_clone_keeps_sender():
    calls = []
    clone = FeishuSendTool(lambda path: calls.append(path) or "ok").clone()

    assert clone.execute("demo.png") == "ok"
    assert calls == ["demo.png"]


def _fake_lark_request_api():
    from lark_oapi.api.im.v1 import (
        CreateFileRequest,
        CreateFileRequestBody,
        CreateImageRequest,
        CreateImageRequestBody,
        PatchMessageRequest,
        PatchMessageRequestBody,
        ReplyMessageRequest,
        ReplyMessageRequestBody,
    )

    return {
        "CreateFileRequest": CreateFileRequest,
        "CreateFileRequestBody": CreateFileRequestBody,
        "CreateImageRequest": CreateImageRequest,
        "CreateImageRequestBody": CreateImageRequestBody,
        "ReplyMessageRequest": ReplyMessageRequest,
        "ReplyMessageRequestBody": ReplyMessageRequestBody,
        "PatchMessageRequest": PatchMessageRequest,
        "PatchMessageRequestBody": PatchMessageRequestBody,
    }

