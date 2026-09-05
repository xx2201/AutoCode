"""Token-budget reminders and summary-free context-window transitions."""

from dataclasses import dataclass

from ..message_content import content_text


def estimate_tokens(messages: list[dict]) -> int:
    """Local text approximation; authoritative provider usage is preferred."""
    return sum(len(content_text(m.get("content", ""))) // 3
               + len(str(m.get("tool_calls", ""))) // 3 for m in messages)


@dataclass
class CompressionResult:
    compressed: bool
    layers: tuple[str, ...]
    before_tokens: int
    after_tokens: int
    before_messages: int
    after_messages: int


class ContextManager:
    def __init__(self, max_tokens=1_000_000, output_reserve_tokens=0,
                 *, reminder_tokens=None, fallback_buffer_tokens=None):
        if not 0 <= output_reserve_tokens < max_tokens:
            raise ValueError("Context window must exceed the nonnegative output reserve")
        self.max_tokens = max_tokens
        self.output_reserve_tokens = output_reserve_tokens
        self.input_budget_tokens = max_tokens - output_reserve_tokens
        # 无模型目录元数据的 provider，以一次最大输出额度作为准备预算；不再按百分比裁历史。
        self.fallback_buffer_tokens = (min(output_reserve_tokens, self.input_budget_tokens - 1)
                                       if fallback_buffer_tokens is None else fallback_buffer_tokens)
        self.reminder_tokens = output_reserve_tokens if reminder_tokens is None else reminder_tokens
        if not 0 <= self.fallback_buffer_tokens < self.input_budget_tokens or self.reminder_tokens < 0:
            raise ValueError("Invalid context preparation budget")
        self.base_limit = self.input_budget_tokens - self.fallback_buffer_tokens

    def status(self, used):
        return {"remaining": max(0, self.base_limit - used),
                "remind": used >= self.base_limit - self.reminder_tokens,
                "fallback": used >= self.base_limit,
                "force": used >= self.input_budget_tokens}

    def maybe_compress(self, messages, *, used_tokens=None, checkpoint=None, force=False):
        # Agent 已校验 usage 锚点并补算增量；这里不得再次用全量字符估算覆盖它。
        before = estimate_tokens(messages) if used_tokens is None else used_tokens
        count = len(messages)
        if not force and not self.status(before)["force"]:
            return CompressionResult(False, (), before, before, count, count)
        if checkpoint is None:
            raise RuntimeError("Window switching requires durable history")
        # 回调先同步证据并构建入口；失败时不得修改现有上下文。
        hint = checkpoint()
        messages[:] = [{"role": "user", "message_kind": "task_context", "content": hint}]
        return CompressionResult(True, ("new_context",), before, estimate_tokens(messages), count, 1)

