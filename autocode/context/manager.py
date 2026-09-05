"""Budgeted window switching backed by durable task notes and raw history."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Callable

from ..message_content import content_text, is_internal_visual_context


def estimate_tokens(messages: list[dict]) -> int:
    """Local text approximation; authoritative usage is preferred."""
    return sum(
        len(content_text(message.get("content", ""))) // 3
        + len(str(message.get("tool_calls", ""))) // 3
        for message in messages
    )


@dataclass
class CompressionResult:
    compressed: bool
    layers: tuple[str, ...]
    before_tokens: int
    after_tokens: int
    before_messages: int
    after_messages: int


class ContextManager:
    def __init__(self, max_tokens: int = 1_000_000, output_reserve_tokens: int = 0):
        if max_tokens <= 0:
            raise ValueError("Context window must be greater than 0.")
        if output_reserve_tokens < 0:
            raise ValueError("Output reserve must not be negative.")
        if output_reserve_tokens >= max_tokens:
            raise ValueError("Output reserve must be smaller than the context window.")
        self.max_tokens = max_tokens
        self.output_reserve_tokens = output_reserve_tokens
        self.input_budget_tokens = max_tokens - output_reserve_tokens
        self._snip_at = int(self.input_budget_tokens * 0.50)
        self._checkpoint_at = int(self.input_budget_tokens * 0.70)
        self._collapse_at = int(self.input_budget_tokens * 0.90)
        self._keep_recent = max(2, min(6, self.input_budget_tokens // 200_000))

    @staticmethod
    def effective_used(messages: list[dict], last_prompt_tokens: int = 0) -> int:
        return max(estimate_tokens(messages), max(0, int(last_prompt_tokens or 0)))

    @staticmethod
    def _is_real_user_turn_start(message: dict) -> bool:
        return (
            message.get("role") == "user"
            and message.get("message_kind") in {None, "user", "prompt"}
            and not is_internal_visual_context(message.get("content"))
            and not content_text(message.get("content", "")).startswith(
                ("[Context compressed - conversation summary]", "[Hard context reset]")
            )
        )

    def _recent_tail(self, messages: list[dict], emergency: bool) -> list[dict]:
        user_indices = [i for i, message in enumerate(messages) if self._is_real_user_turn_start(message)]
        if len(user_indices) > self._keep_recent and not emergency:
            return messages[user_indices[-self._keep_recent]:]
        if emergency and user_indices:
            start = user_indices[-1]
            # 长单轮任务也能切窗，但必须保留当前用户原文及完整的最近工具批次。
            tail_start = max(start + 1, len(messages) - 4)
            while tail_start > start + 1 and messages[tail_start].get("role") == "tool":
                tail_start -= 1
            return [messages[start], *messages[tail_start:]]
        return list(messages)

    @staticmethod
    def _snip_tool_outputs(messages: list[dict]) -> bool:
        changed = False
        for message in messages:
            if message.get("role") != "tool":
                continue
            text = content_text(message.get("content", ""))
            if len(text) <= 1800:
                continue
            source = message.get("message_id", "")
            message["content"] = (
                text[:700] + "\n[Output shortened; read_history message_id="
                + source + " for original evidence]\n" + text[-700:]
            )
            # 图片不因文本裁剪而丢失，model_content 保持原样。
            changed = True
        return changed

    def maybe_compress(
        self,
        messages: list[dict],
        last_prompt_tokens: int = 0,
        *,
        checkpoint: Callable[[], str] | None = None,
    ) -> CompressionResult:
        before_tokens, before_messages = estimate_tokens(messages), len(messages)
        current = self.effective_used(messages, last_prompt_tokens)
        draft = deepcopy(messages)
        layers = []
        if current > self._snip_at and self._snip_tool_outputs(draft):
            layers.append("tool_snip")
        if current > self._checkpoint_at:
            tail = self._recent_tail(draft, emergency=current > self._collapse_at)
            if len(tail) < len(draft) or any(m.get("message_kind") == "task_context" for m in tail):
                if checkpoint is None:
                    raise RuntimeError("Window switching requires a durable task-notes checkpoint")
                # 保存 notes 与增量游标成功后，才能替换当前运行窗口。
                notes = checkpoint()
                tail = [m for m in tail if m.get("message_kind") != "task_context"]
                draft = [{"role": "user", "message_kind": "task_context", "content": notes}, *tail]
                layers.append("task_checkpoint")
        if layers:
            if checkpoint is None:
                raise RuntimeError("Context trimming requires durable history")
            if "task_checkpoint" not in layers:
                checkpoint()
            messages[:] = draft
        return CompressionResult(
            bool(layers), tuple(layers), before_tokens, estimate_tokens(messages),
            before_messages, len(messages),
        )

