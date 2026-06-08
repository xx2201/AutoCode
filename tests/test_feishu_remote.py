from autocode.config import Config
from autocode.remote.feishu_bot import _build_api_client, _build_patch_request, _build_reply_request, _session_key
from autocode.remote.feishu_formatting import (
    build_approval_card,
    build_live_status_card,
    build_text_content,
    parse_text_content,
    split_text_chunks,
)
from autocode.remote.manager import RemoteTurnResult


def test_parse_text_content_reads_feishu_json():
    assert parse_text_content('{"text":"hello"}') == "hello"
    assert parse_text_content("plain text") == "plain text"


def test_build_text_content_preserves_unicode():
    assert build_text_content("hello 世界") == '{"text": "hello 世界"}'


def test_build_approval_card_embeds_actions():
    result = RemoteTurnResult(
        text="waiting for approval",
        task_id="task_123",
        status="waiting_approval",
        pending_tool="bash",
        pending_reason="command is not in allowlist",
        pending_arguments={"command": "python app.py"},
        pending_requires_manual=True,
        auto_approve_for_task=False,
    )
    card = build_approval_card(result, "user:ou_xxx", "ou_owner")
    actions = []
    for column in card["body"]["elements"][1]["columns"]:
        actions.append(column["elements"][0]["value"]["command"])
    assert actions == ["approve", "approve_all", "reject"]
    assert "python app.py" in card["body"]["elements"][0]["content"]
    assert "Approve_all" in card["body"]["elements"][0]["content"]


def test_build_live_status_card_shows_runtime_progress():
    card = build_live_status_card(
        title="Fix import",
        phase="Running Tool",
        status="running",
        task_id="task_123",
        step_index=2,
        llm_calls=1,
        tool_calls=1,
        prompt_tokens=120,
        completion_tokens=40,
        last_tool="read_file",
        detail="Executing read_file.",
        auto_approve_for_task=False,
    )
    content = card["body"]["elements"][0]["content"]
    assert "Running Tool" in content
    assert "read_file" in content
    assert "task_123" in content


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


def _fake_lark_request_api():
    from lark_oapi.api.im.v1 import (
        PatchMessageRequest,
        PatchMessageRequestBody,
        ReplyMessageRequest,
        ReplyMessageRequestBody,
    )

    return {
        "ReplyMessageRequest": ReplyMessageRequest,
        "ReplyMessageRequestBody": ReplyMessageRequestBody,
        "PatchMessageRequest": PatchMessageRequest,
        "PatchMessageRequestBody": PatchMessageRequestBody,
    }

