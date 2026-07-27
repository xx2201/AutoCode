"""Feishu bot adapter for AutoCode remote control."""

from __future__ import annotations

import json
import logging
import threading
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from ..config import Config
from ..mcp import get_shared_mcp_manager
from ..tools.factory import build_agent_tools
from .feishu_formatting import (
    build_approval_card,
    build_error_card,
    build_file_content,
    build_image_content,
    build_live_status_card,
    build_resume_card,
    build_text_content,
    parse_text_content,
    render_text_result,
    split_text_chunks,
)
from .manager import RemoteManager, RemoteTurnResult
from .feishu_tool import FeishuSendTool

logger = logging.getLogger(__name__)
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".ico", ".tif", ".tiff", ".heic"}
_FILE_TYPE_MAP = {
    ".opus": "opus",
    ".mp4": "mp4",
    ".pdf": "pdf",
    ".doc": "doc",
    ".docx": "doc",
    ".xls": "xls",
    ".xlsx": "xls",
    ".ppt": "ppt",
    ".pptx": "ppt",
}


@dataclass
class _ResumeSelection:
    tasks: list[dict]
    session_key: str
    owner_open_id: str


def main():
    config = Config.from_env()
    _validate_config(config)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    FeishuBot(config).start()


class FeishuBot:
    def __init__(self, config: Config):
        self.config = config
        self.lark = _import_lark()
        self._tool_context = threading.local()
        mcp_manager = get_shared_mcp_manager(config.workspace_root, config.mcp_config_path)
        self.manager = RemoteManager(
            config,
            tool_factory=lambda: build_agent_tools(
                config,
                extra_tools=[FeishuSendTool(self._send_attachment_from_tool)],
                mcp_manager=mcp_manager,
            ),
        )
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="autocode-feishu")
        self._live_states: dict[str, _LiveStatus] = {}
        self.client = _build_api_client(self.lark, config)
        logger.info(
            "Feishu API domain=%s message_domain=%s",
            getattr(getattr(self.client, "_config", None), "domain", None),
            getattr(getattr(self.client.im.v1.message, "config", None), "domain", None),
        )
        self.dispatcher = self._build_dispatcher()

    def start(self):
        ws_client = self.lark["ws"].Client(
            self.config.feishu_app_id,
            self.config.feishu_app_secret,
            log_level=self.lark["LogLevel"].INFO,
            event_handler=self.dispatcher,
        )
        ws_client.start()

    def _build_dispatcher(self):
        return (
            self.lark["EventDispatcherHandler"]
            .builder("", "")
            .register_p2_im_message_receive_v1(self._on_message)
            .register_p2_card_action_trigger(self._on_card_action)
            .build()
        )

    def _on_message(self, data) -> None:
        event = getattr(data, "event", None)
        message = getattr(event, "message", None)
        sender = getattr(event, "sender", None)
        if message is None or sender is None:
            return
        if getattr(sender, "sender_type", "") != "user":
            return

        sender_open_id = getattr(getattr(sender, "sender_id", None), "open_id", "") or ""
        chat_id = getattr(message, "chat_id", "") or ""
        if not self._is_allowed(sender_open_id, chat_id):
            logger.info("ignored unauthorized feishu sender open_id=%s chat_id=%s", sender_open_id, chat_id)
            return

        self.executor.submit(self._handle_message, message, sender_open_id)

    def _on_card_action(self, data):
        response = self.lark["P2CardActionTriggerResponse"]({})
        response.toast = self.lark["CallBackToast"](
            {"type": "info", "content": "Approval received. Processing..."}
        )

        event = getattr(data, "event", None)
        action = getattr(event, "action", None)
        context = getattr(event, "context", None)
        operator = getattr(event, "operator", None)
        value = getattr(action, "value", None) or {}

        sender_open_id = getattr(operator, "open_id", "") or ""
        chat_id = getattr(context, "open_chat_id", "") or ""
        owner_open_id = value.get("owner_open_id", "")
        if not self._is_allowed(sender_open_id, chat_id):
            response.toast = self.lark["CallBackToast"]({"type": "warning", "content": "Not allowed."})
            return response
        if owner_open_id and sender_open_id != owner_open_id:
            response.toast = self.lark["CallBackToast"](
                {"type": "warning", "content": "Only the task owner can approve."}
            )
            return response

        command = value.get("command", "")
        session_key = value.get("session_key", "")
        message_id = getattr(context, "open_message_id", "") or ""
        session_id = value.get("session_id", "")
        if command not in {"approve", "approve_scope", "reject", "resume"} or not session_key or not message_id:
            response.toast = self.lark["CallBackToast"]({"type": "warning", "content": "Invalid action payload."})
            return response
        if command == "resume" and not session_id:
            response.toast = self.lark["CallBackToast"]({"type": "warning", "content": "Missing session id."})
            return response

        self.executor.submit(self._handle_card_action, session_key, message_id, session_id, command, sender_open_id)
        return response

    def _handle_message(self, message, sender_open_id: str):
        message_id = getattr(message, "message_id", "") or ""
        if not message_id:
            return
        if getattr(message, "message_type", "") != "text":
            self._reply_text(message_id, "Only text messages are supported right now.")
            return

        text = parse_text_content(getattr(message, "content", "") or "")
        if not text:
            self._reply_text(message_id, "Empty messages are ignored.")
            return

        session_key = _session_key(message, sender_open_id)
        try:
            with self._bind_reply_target(message_id):
                if text.startswith("/"):
                    result = self._handle_text(session_key, text, sender_open_id)
                else:
                    result = self._run_live_task(message_id, session_key, sender_open_id, text)
            self._deliver_result(message_id, result, session_key, sender_open_id)
        except Exception as exc:
            logger.exception("Feishu message handling failed")
            self._reply_text(message_id, f"Error: {exc}")

    def _handle_card_action(
        self,
        session_key: str,
        message_id: str,
        session_id: str,
        command: str,
        sender_open_id: str,
    ):
        try:
            with self._bind_reply_target(message_id):
                if command == "resume":
                    result = self.manager.resume_session(session_key, session_id)
                    self._deliver_result(message_id, result, session_key, sender_open_id)
                    return
                live = self._live_states.get(session_key) or _LiveStatus.from_pending(
                    session_key=session_key,
                    owner_open_id=sender_open_id,
                    message_id=message_id,
                    title=f"Session {session_id or ''}".strip(),
                )
                live.message_id = message_id
                live.phase = "Continuing"
                live.detail = f"Action: {command}"
                self._patch_live_card(live, force=True)
                result = self._resolve_command(session_key, command, hook_handler=self._make_live_hook(live))
            self._deliver_result(message_id, result, session_key, sender_open_id)
        except Exception as exc:
            logger.exception("Feishu card action failed")
            self._reply_card(message_id, build_error_card("AutoCode Error", f"Error: {exc}"))

    def _handle_text(self, session_key: str, text: str, sender_open_id: str) -> RemoteTurnResult | str | _ResumeSelection:
        try:
            if not text.startswith("/"):
                return self.manager.submit(session_key, text)

            command, _, _ = text.partition(" ")
            if command in {"/start", "/help"}:
                return _help_text()
            if command == "/reset":
                self.manager.reset_chat(session_key)
                return "Chat session cleared."
            if command == "/task":
                return self.manager.current_task_summary(session_key)
            if command == "/trace":
                return self.manager.current_trace(session_key)
            if command == "/resume":
                tasks = self.manager.list_resume_candidates(limit=10)
                if not tasks:
                    return "No resumable sessions for the current project."
                return _ResumeSelection(tasks=tasks, session_key=session_key, owner_open_id=sender_open_id)
            if command in {"/approve", "/approve_scope", "/reject"}:
                live = self._live_states.get(session_key)
                hook_handler = self._make_live_hook(live) if live else None
                if live:
                    live.phase = "Continuing"
                    live.detail = f"Action: {command[1:]}"
                    self._patch_live_card(live, force=True)
                return self._resolve_command(session_key, command[1:], hook_handler=hook_handler)
            return "Unknown command. Send /help for available commands."
        except ValueError as exc:
            return str(exc)

    def _resolve_command(self, session_key: str, command: str, hook_handler=None) -> RemoteTurnResult:
        if command == "approve":
            return self.manager.resolve_approval(session_key, approved=True, hook_handler=hook_handler)
        if command == "approve_scope":
            return self.manager.resolve_approval(
                session_key,
                approved=True,
                grant_scope=True,
                hook_handler=hook_handler,
            )
        if command == "reject":
            return self.manager.resolve_approval(session_key, approved=False, hook_handler=hook_handler)
        raise ValueError(f"Unsupported command: {command}")

    def _deliver_result(
        self,
        message_id: str,
        result: RemoteTurnResult | str | _ResumeSelection,
        session_key: str,
        sender_open_id: str,
    ):
        if isinstance(result, _ResumeSelection):
            self._reply_card(
                message_id,
                build_resume_card(
                    result.tasks,
                    session_key=result.session_key,
                    owner_open_id=result.owner_open_id,
                    workspace_root=self.config.workspace_root,
                ),
            )
            return
        if isinstance(result, str):
            for chunk in split_text_chunks(result):
                self._reply_text(message_id, chunk)
            return
        live = self._live_states.get(session_key)
        if result.pending_tool:
            if live:
                live.session_id = result.session_id or live.session_id
                live.task_id = result.task_id or live.task_id
                live.status = result.status or live.status
                live.permission_mode = result.permission_mode
                live.phase = "Waiting Approval"
                live.detail = result.pending_reason or "approval required"
                live.last_tool = result.pending_tool
                live.owner_open_id = sender_open_id or live.owner_open_id
                live.message_id = live.message_id or message_id
                self._patch_card(live.message_id, build_approval_card(result, session_key, live.owner_open_id))
                return
            self._reply_card(message_id, build_approval_card(result, session_key, sender_open_id))
            return
        if live:
            live.session_id = result.session_id or live.session_id
            live.task_id = result.task_id or live.task_id
            live.status = result.status or live.status or "completed"
            live.permission_mode = result.permission_mode
            live.phase = "Completed"
            live.detail = "Final result sent below."
            self._patch_live_card(live, force=True, template="green")
        for chunk in render_text_result(result):
            self._reply_text(message_id, chunk)

    def _run_live_task(self, message_id: str, session_key: str, sender_open_id: str, text: str) -> RemoteTurnResult:
        live = _LiveStatus(
            title=text.splitlines()[0][:120],
            owner_open_id=sender_open_id,
            session_key=session_key,
        )
        live.message_id = self._reply_card(message_id, build_live_status_card(**live.as_card_kwargs()))
        self._live_states[session_key] = live
        hook_handler = self._make_live_hook(live)
        return self.manager.submit(session_key, text, hook_handler=hook_handler)

    def _make_live_hook(self, live: "_LiveStatus"):
        def _handler(event: str, payload: dict):
            live.handle(event, payload)
            self._patch_live_card(live)

        return _handler

    def _patch_live_card(self, live: "_LiveStatus", force: bool = False, template: str | None = None):
        if not live.message_id:
            return
        now = time.monotonic()
        if not force and now - live.last_patch_at < 0.8:
            return
        live.last_patch_at = now
        card_kwargs = live.as_card_kwargs()
        if template:
            card_kwargs["template"] = template
        self._patch_card(live.message_id, build_live_status_card(**card_kwargs))

    def _reply_text(self, message_id: str, text: str):
        request = _build_reply_request(self.lark, message_id, "text", build_text_content(text))
        response = self.client.im.v1.message.reply(request)
        if not response.success():
            raise RuntimeError(f"Feishu reply failed: {response.code} {response.msg}")
        return getattr(getattr(response, "data", None), "message_id", "") or ""

    def _reply_image(self, message_id: str, path: Path):
        image_key = self._upload_image(path)
        request = _build_reply_request(self.lark, message_id, "image", build_image_content(image_key))
        response = self.client.im.v1.message.reply(request)
        if not response.success():
            raise RuntimeError(f"Feishu image reply failed: {response.code} {response.msg}")
        return getattr(getattr(response, "data", None), "message_id", "") or ""

    def _reply_file(self, message_id: str, path: Path):
        file_key = self._upload_file(path)
        request = _build_reply_request(self.lark, message_id, "file", build_file_content(file_key, path.name))
        response = self.client.im.v1.message.reply(request)
        if not response.success():
            raise RuntimeError(f"Feishu file reply failed: {response.code} {response.msg}")
        return getattr(getattr(response, "data", None), "message_id", "") or ""

    def _reply_card(self, message_id: str, card: dict):
        request = _build_reply_request(self.lark, message_id, "interactive", json.dumps(card, ensure_ascii=False))
        response = self.client.im.v1.message.reply(request)
        if not response.success():
            raise RuntimeError(f"Feishu card reply failed: {response.code} {response.msg}")
        return getattr(getattr(response, "data", None), "message_id", "") or ""

    def _patch_card(self, message_id: str, card: dict):
        request = _build_patch_request(self.lark, message_id, json.dumps(card, ensure_ascii=False))
        response = self.client.im.v1.message.patch(request)
        if not response.success():
            raise RuntimeError(f"Feishu card patch failed: {response.code} {response.msg}")

    def _upload_image(self, path: Path) -> str:
        with path.open("rb") as file_obj:
            request = _build_image_upload_request(self.lark, file_obj)
            response = self.client.im.v1.image.create(request)
        if not response.success():
            raise RuntimeError(f"Feishu image upload failed: {response.code} {response.msg}")
        image_key = getattr(getattr(response, "data", None), "image_key", "") or ""
        if not image_key:
            raise RuntimeError("Feishu image upload returned an empty image_key.")
        return image_key

    def _upload_file(self, path: Path) -> str:
        with path.open("rb") as file_obj:
            request = _build_file_upload_request(self.lark, file_obj, path.name)
            response = self.client.im.v1.file.create(request)
        if not response.success():
            raise RuntimeError(f"Feishu file upload failed: {response.code} {response.msg}")
        file_key = getattr(getattr(response, "data", None), "file_key", "") or ""
        if not file_key:
            raise RuntimeError("Feishu file upload returned an empty file_key.")
        return file_key

    def _send_attachment_from_tool(self, file_path: str) -> str:
        message_id = getattr(self._tool_context, "message_id", "") or ""
        if not message_id:
            raise RuntimeError("No active Feishu reply target.")
        path = _resolve_workspace_attachment(self.config.workspace_root, file_path)
        if _attachment_kind(path) == "image":
            self._reply_image(message_id, path)
            return f"Sent image to Feishu chat: {path.name}"
        self._reply_file(message_id, path)
        return f"Sent file to Feishu chat: {path.name}"

    @contextmanager
    def _bind_reply_target(self, message_id: str):
        previous = getattr(self._tool_context, "message_id", None)
        self._tool_context.message_id = message_id
        try:
            yield
        finally:
            if previous is None:
                try:
                    del self._tool_context.message_id
                except AttributeError:
                    pass
            else:
                self._tool_context.message_id = previous

    def _is_allowed(self, sender_open_id: str, chat_id: str) -> bool:
        allowed_open_ids = set(self.config.feishu_allowed_open_ids)
        allowed_chat_ids = set(self.config.feishu_allowed_chat_ids)
        if allowed_open_ids and sender_open_id not in allowed_open_ids:
            return False
        if allowed_chat_ids and chat_id not in allowed_chat_ids:
            return False
        return True


def _help_text() -> str:
    return (
        "AutoCode Feishu control is ready.\n\n"
        "Commands:\n"
        "/task - show current session and task\n"
        "/trace - show the current session trace\n"
        "/approve - approve the pending tool call\n"
        "/approve_scope - approve and allow this scope for the current task\n"
        "/reject - reject the pending tool call\n"
        "/resume - show resumable sessions for the current project\n"
        "/reset - clear the in-memory chat session\n\n"
        "Any non-command text is sent to the coding agent."
    )


def _session_key(message, sender_open_id: str) -> str:
    if getattr(message, "chat_type", "") == "p2p":
        return f"user:{sender_open_id}"
    return f"chat:{getattr(message, 'chat_id', '')}"


def _validate_config(config: Config):
    if not config.model:
        raise SystemExit("No model configured. Set AUTOCODE_MODEL before starting Feishu control.")
    if not config.api_key:
        raise SystemExit("No API key configured. Set AUTOCODE_API_KEY before starting Feishu control.")
    if not config.feishu_app_id:
        raise SystemExit("No Feishu app id configured. Set AUTOCODE_FEISHU_APP_ID.")
    if not config.feishu_app_secret:
        raise SystemExit("No Feishu app secret configured. Set AUTOCODE_FEISHU_APP_SECRET.")


def _build_api_client(lark_api: dict, config: Config):
    client = (
        lark_api["Client"]
        .builder()
        .app_id(config.feishu_app_id)
        .app_secret(config.feishu_app_secret)
        .domain(lark_api["FEISHU_DOMAIN"])
        .build()
    )
    client._config.domain = lark_api["FEISHU_DOMAIN"]
    client.im.v1.message.config.domain = lark_api["FEISHU_DOMAIN"]
    return client


def _build_reply_request(lark_api: dict, message_id: str, msg_type: str, content: str):
    body = (
        lark_api["ReplyMessageRequestBody"]
        .builder()
        .msg_type(msg_type)
        .content(content)
        .uuid(str(uuid4()))
        .build()
    )
    return lark_api["ReplyMessageRequest"].builder().message_id(message_id).request_body(body).build()


def _build_patch_request(lark_api: dict, message_id: str, content: str):
    body = lark_api["PatchMessageRequestBody"].builder().content(content).build()
    return lark_api["PatchMessageRequest"].builder().message_id(message_id).request_body(body).build()


def _build_image_upload_request(lark_api: dict, file_obj):
    body = lark_api["CreateImageRequestBody"].builder().image_type("message").image(file_obj).build()
    return lark_api["CreateImageRequest"].builder().request_body(body).build()


def _build_file_upload_request(lark_api: dict, file_obj, file_name: str):
    body = (
        lark_api["CreateFileRequestBody"]
        .builder()
        .file_type(_guess_feishu_file_type(file_name))
        .file_name(file_name)
        .file(file_obj)
        .build()
    )
    return lark_api["CreateFileRequest"].builder().request_body(body).build()


def _guess_feishu_file_type(file_name: str) -> str:
    return _FILE_TYPE_MAP.get(Path(file_name).suffix.lower(), "stream")


def _attachment_kind(path: Path) -> str:
    return "image" if path.suffix.lower() in _IMAGE_EXTENSIONS else "file"


def _resolve_workspace_attachment(workspace_root: str, raw_path: str) -> Path:
    if not raw_path:
        raise ValueError("Missing file path.")
    workspace = Path(workspace_root).expanduser().resolve()
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = workspace / path
    path = path.resolve()
    try:
        path.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(f"path must stay inside workspace: {workspace}") from exc
    if not path.is_file():
        raise ValueError(f"File not found: {path}")
    return path


def _import_lark():
    try:
        import lark_oapi as lark
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
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            CallBackToast,
            P2CardActionTriggerResponse,
        )
    except ImportError as exc:
        raise SystemExit(
            "Feishu support is optional. Install it with: pip install -e '.[feishu]'"
        ) from exc
    return {
        "Client": lark.Client,
        "CreateFileRequest": CreateFileRequest,
        "CreateFileRequestBody": CreateFileRequestBody,
        "CreateImageRequest": CreateImageRequest,
        "CreateImageRequestBody": CreateImageRequestBody,
        "EventDispatcherHandler": lark.EventDispatcherHandler,
        "LogLevel": lark.LogLevel,
        "PatchMessageRequest": PatchMessageRequest,
        "PatchMessageRequestBody": PatchMessageRequestBody,
        "ReplyMessageRequest": ReplyMessageRequest,
        "ReplyMessageRequestBody": ReplyMessageRequestBody,
        "P2CardActionTriggerResponse": P2CardActionTriggerResponse,
        "CallBackToast": CallBackToast,
        "FEISHU_DOMAIN": lark.FEISHU_DOMAIN,
        "ws": lark.ws,
    }


@dataclass
class _LiveStatus:
    title: str
    owner_open_id: str
    session_key: str
    message_id: str = ""
    session_id: str = ""
    task_id: str = ""
    status: str = "running"
    phase: str = "Starting"
    detail: str = "Task received. Preparing the agent runtime..."
    step_index: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_miss_tokens: int = 0
    last_prompt_tokens: int = 0
    last_completion_tokens: int = 0
    last_cache_read_tokens: int = 0
    last_cache_miss_tokens: int = 0
    compactions: int = 0
    cache_segments: int = 1
    last_tool: str = ""
    permission_mode: str = "ask"
    last_patch_at: float = 0.0

    @classmethod
    def from_pending(cls, *, session_key: str, owner_open_id: str, message_id: str, title: str):
        return cls(title=title or "(untitled task)", owner_open_id=owner_open_id, session_key=session_key, message_id=message_id)

    def handle(self, event: str, payload: dict):
        self.session_id = payload.get("session_id", self.session_id)
        self.task_id = payload.get("task_id", self.task_id)
        if event == "before_llm":
            self.phase = "Thinking"
            self.step_index = int(payload.get("step_index", self.step_index))
            self.status = "running"
            self.detail = "Calling the model for the next step."
        elif event == "after_llm":
            self.llm_calls += 1
            self.step_index = int(payload.get("step_index", self.step_index))
            self.last_prompt_tokens = int(payload.get("prompt_tokens", 0))
            self.last_completion_tokens = int(payload.get("completion_tokens", 0))
            self.last_cache_read_tokens = int(payload.get("cache_read_tokens", 0))
            self.last_cache_miss_tokens = int(payload.get("cache_miss_tokens", 0))
            self.prompt_tokens += self.last_prompt_tokens
            self.completion_tokens += self.last_completion_tokens
            self.cache_read_tokens += self.last_cache_read_tokens
            self.cache_miss_tokens += self.last_cache_miss_tokens
            tool_calls = int(payload.get("tool_calls", 0))
            self.phase = "Planning" if tool_calls else "Answering"
            self.detail = f"Model returned {tool_calls} tool call(s)." if tool_calls else "Model returned a direct answer."
        elif event == "context_compaction":
            self.compactions += 1
            self.cache_segments = 1 + self.compactions
            self.phase = "Compacting"
            self.detail = (
                f"Context compacted: saved {int(payload.get('saved_tokens', 0))} tokens "
                f"via {', '.join(payload.get('layers', [])) or 'compression'}."
            )
        elif event == "before_tool":
            self.tool_calls += 1
            self.last_tool = payload.get("tool_name", self.last_tool)
            self.phase = "Running Tool"
            self.detail = f"Executing {self.last_tool}."
        elif event == "after_tool":
            self.phase = "Tool Finished"
            self.detail = f"{payload.get('tool_name', self.last_tool)} completed."
        elif event == "policy_decision":
            decision = payload.get("decision", {})
            self.last_tool = payload.get("tool_name", self.last_tool)
            action = decision.get("action", "")
            if action == "confirm":
                self.phase = "Waiting Approval"
                self.detail = decision.get("reason", "Approval required.")
            elif action == "deny":
                self.phase = "Blocked"
                self.detail = decision.get("reason", "Blocked by policy.")
        elif event == "approval_resolved":
            self.phase = "Continuing"
            self.detail = "Approval resolved. Continuing the task."
        elif event == "task_status":
            self.status = payload.get("status", self.status)
            if self.status == "completed":
                self.phase = "Completed"
            elif self.status == "waiting_approval":
                self.phase = "Waiting Approval"
        elif event == "task_error":
            self.status = payload.get("status", "failed")
            self.phase = "Failed"
            self.detail = payload.get("error", "Task failed.")

    def as_card_kwargs(self) -> dict:
        return {
            "title": self.title,
            "phase": self.phase,
            "status": self.status,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "step_index": self.step_index,
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "last_prompt_tokens": self.last_prompt_tokens,
            "last_completion_tokens": self.last_completion_tokens,
            "last_cache_read_tokens": self.last_cache_read_tokens,
            "last_cache_miss_tokens": self.last_cache_miss_tokens,
            "compactions": self.compactions,
            "cache_segments": self.cache_segments,
            "last_tool": self.last_tool,
            "detail": self.detail,
            "permission_mode": self.permission_mode,
        }


if __name__ == "__main__":
    main()

