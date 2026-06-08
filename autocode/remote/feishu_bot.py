"""Feishu bot adapter for AutoCode remote control."""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from uuid import uuid4

from ..config import Config
from .feishu_formatting import (
    build_action_status_card,
    build_approval_card,
    build_error_card,
    build_live_status_card,
    build_text_content,
    parse_text_content,
    render_text_result,
    split_text_chunks,
)
from .formatting import render_task_list
from .manager import RemoteManager, RemoteTurnResult

logger = logging.getLogger(__name__)


def main():
    config = Config.from_env()
    _validate_config(config)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    FeishuBot(config).start()


class FeishuBot:
    def __init__(self, config: Config):
        self.config = config
        self.lark = _import_lark()
        self.manager = RemoteManager(config)
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
        task_id = value.get("task_id", "")
        if command not in {"approve", "approve_all", "reject"} or not session_key or not message_id:
            response.toast = self.lark["CallBackToast"]({"type": "warning", "content": "Invalid action payload."})
            return response

        self.executor.submit(self._handle_card_action, session_key, message_id, task_id, command, sender_open_id)
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
            if text.startswith("/"):
                result = self._handle_text(session_key, text)
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
        task_id: str,
        command: str,
        sender_open_id: str,
    ):
        try:
            live = self._live_states.get(session_key) or _LiveStatus.from_pending(
                session_key=session_key,
                owner_open_id=sender_open_id,
                message_id=message_id,
                title=f"Task {task_id or ''}".strip(),
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

    def _handle_text(self, session_key: str, text: str) -> RemoteTurnResult | str:
        try:
            if not text.startswith("/"):
                return self.manager.submit(session_key, text)

            command, _, arg_text = text.partition(" ")
            arg_text = arg_text.strip()
            if command in {"/start", "/help"}:
                return _help_text()
            if command == "/reset":
                self.manager.reset_chat(session_key)
                return "Chat session cleared."
            if command == "/task":
                return self.manager.current_task_summary(session_key)
            if command == "/tasks":
                return render_task_list(self.manager.list_recent_tasks())
            if command == "/trace":
                return self.manager.current_trace(session_key)
            if command in {"/approve", "/approve_all", "/reject"}:
                live = self._live_states.get(session_key)
                hook_handler = self._make_live_hook(live) if live else None
                if live:
                    live.phase = "Continuing"
                    live.detail = f"Action: {command[1:]}"
                    self._patch_live_card(live, force=True)
                return self._resolve_command(session_key, command[1:], hook_handler=hook_handler)
            if command == "/resume":
                if not arg_text:
                    return "Usage: /resume <task_id>"
                return self.manager.resume_task(session_key, arg_text)
            return "Unknown command. Send /help for available commands."
        except ValueError as exc:
            return str(exc)

    def _resolve_command(self, session_key: str, command: str, hook_handler=None) -> RemoteTurnResult:
        if command == "approve":
            return self.manager.resolve_approval(session_key, approved=True, hook_handler=hook_handler)
        if command == "approve_all":
            return self.manager.resolve_approval(
                session_key,
                approved=True,
                enable_auto_approve=True,
                hook_handler=hook_handler,
            )
        if command == "reject":
            return self.manager.resolve_approval(session_key, approved=False, hook_handler=hook_handler)
        raise ValueError(f"Unsupported command: {command}")

    def _deliver_result(
        self,
        message_id: str,
        result: RemoteTurnResult | str,
        session_key: str,
        sender_open_id: str,
    ):
        if isinstance(result, str):
            for chunk in split_text_chunks(result):
                self._reply_text(message_id, chunk)
            return
        live = self._live_states.get(session_key)
        if result.pending_tool:
            if live:
                live.task_id = result.task_id or live.task_id
                live.status = result.status or live.status
                live.auto_approve_for_task = result.auto_approve_for_task
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
            live.task_id = result.task_id or live.task_id
            live.status = result.status or live.status or "completed"
            live.auto_approve_for_task = result.auto_approve_for_task
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
        "/task - show current task\n"
        "/tasks - list recent checkpoints\n"
        "/trace - show the current trace\n"
        "/approve - approve the pending tool call\n"
        "/approve_all - approve this tool call and auto-approve later normal confirms\n"
        "/reject - reject the pending tool call\n"
        "/resume <task_id> - restore a checkpoint into this chat\n"
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


def _import_lark():
    try:
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import (
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
    task_id: str = ""
    status: str = "running"
    phase: str = "Starting"
    detail: str = "Task received. Preparing the agent runtime..."
    step_index: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    last_tool: str = ""
    auto_approve_for_task: bool = False
    last_patch_at: float = 0.0

    @classmethod
    def from_pending(cls, *, session_key: str, owner_open_id: str, message_id: str, title: str):
        return cls(title=title or "(untitled task)", owner_open_id=owner_open_id, session_key=session_key, message_id=message_id)

    def handle(self, event: str, payload: dict):
        self.task_id = payload.get("task_id", self.task_id)
        if event == "before_llm":
            self.phase = "Thinking"
            self.step_index = int(payload.get("step_index", self.step_index))
            self.status = "running"
            self.detail = "Calling the model for the next step."
        elif event == "after_llm":
            self.llm_calls += 1
            self.step_index = int(payload.get("step_index", self.step_index))
            self.prompt_tokens += int(payload.get("prompt_tokens", 0))
            self.completion_tokens += int(payload.get("completion_tokens", 0))
            tool_calls = int(payload.get("tool_calls", 0))
            self.phase = "Planning" if tool_calls else "Answering"
            self.detail = f"Model returned {tool_calls} tool call(s)." if tool_calls else "Model returned a direct answer."
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
            "task_id": self.task_id,
            "step_index": self.step_index,
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "last_tool": self.last_tool,
            "detail": self.detail,
            "auto_approve_for_task": self.auto_approve_for_task,
        }


if __name__ == "__main__":
    main()

