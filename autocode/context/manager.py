"""Multi-layer context compression.

Claude Code uses a 4-layer strategy:
  1. HISTORY_SNIP   - trim old tool outputs to a one-line summary
  2. Microcompact   - LLM-powered summary of old turns (cached)
  3. CONTEXT_COLLAPSE - aggressive compression when nearing hard limit
  4. Autocompact    - periodic background compaction

AutoCode implements the same idea in 3 layers:
  Layer 1 (tool_snip)   - replace verbose tool results with truncated versions
  Layer 2 (summarize)   - LLM-powered summary of old conversation
  Layer 3 (hard_collapse) - last resort: drop everything except summary + recent
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..message_content import content_text, is_internal_visual_context

if TYPE_CHECKING:
    from ..llm import LLM


def _approx_tokens(text: str) -> int:
    """Rough token count. ~3.5 chars/token for mixed en/zh content."""
    return len(text) // 3


def estimate_tokens(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        if m.get("content"):
            total += _approx_tokens(content_text(m["content"]))
        if m.get("tool_calls"):
            total += _approx_tokens(str(m["tool_calls"]))
    return total


@dataclass
class CompressionResult:
    compressed: bool
    layers: tuple[str, ...]
    before_tokens: int
    after_tokens: int
    before_messages: int
    after_messages: int


class ContextManager:
    def __init__(self, max_tokens: int = 1_000_000):
        self.max_tokens = max_tokens
        # layer thresholds (fraction of max_tokens)
        self._snip_at = int(max_tokens * 0.50)    # 50% -> snip tool outputs
        self._summarize_at = int(max_tokens * 0.70)  # 70% -> LLM summarize
        self._collapse_at = int(max_tokens * 0.90)   # 90% -> hard collapse
        self._summary_keep_recent = max(2, min(6, max_tokens // 200_000))
        self._collapse_keep_recent = max(1, min(3, max_tokens // 400_000))
        self._summary_input_chars = max(15_000, min(120_000, max_tokens // 6))

    @staticmethod
    def effective_used(messages: list[dict], last_prompt_tokens: int = 0) -> int:
        return max(estimate_tokens(messages), max(0, int(last_prompt_tokens or 0)))

    def maybe_compress(
        self,
        messages: list[dict],
        llm: LLM | None = None,
        last_prompt_tokens: int = 0,
    ) -> CompressionResult:
        """Apply compression layers as needed and return structured stats."""
        before_tokens = estimate_tokens(messages)
        before_messages = len(messages)
        current = self.effective_used(messages, last_prompt_tokens=last_prompt_tokens)
        layers: list[str] = []

        # Layer 1: snip verbose tool outputs
        if current > self._snip_at:
            if self._snip_tool_outputs(messages):
                layers.append("tool_snip")
                current = estimate_tokens(messages)

        # Layer 2: LLM-powered summarization of old turns
        if current > self._summarize_at and len(messages) > self._summary_keep_recent:
            if self._summarize_old(messages, llm, keep_recent=self._summary_keep_recent):
                layers.append("summarize_old")
                current = estimate_tokens(messages)

        # Layer 3: hard collapse - last resort
        if current > self._collapse_at and len(messages) > self._collapse_keep_recent:
            self._hard_collapse(messages, llm)
            layers.append("hard_collapse")

        return CompressionResult(
            compressed=bool(layers),
            layers=tuple(layers),
            before_tokens=before_tokens,
            after_tokens=estimate_tokens(messages),
            before_messages=before_messages,
            after_messages=len(messages),
        )

    @staticmethod
    def _snip_tool_outputs(messages: list[dict]) -> bool:
        """Layer 1: Truncate tool results over 1500 chars to their first/last lines.

        This mirrors Claude Code's HISTORY_SNIP which replaces old tool outputs
        with a one-line summary to reclaim context space.
        """
        changed = False
        for m in messages:
            if m.get("role") != "tool":
                continue
            content = m.get("content", "")
            if len(content) <= 1500:
                continue
            lines = content.splitlines()
            if len(lines) <= 6:
                continue
            # keep first 3 + last 3 lines
            snipped = (
                "\n".join(lines[:3])
                + f"\n... ({len(lines)} lines, snipped to save context) ...\n"
                + "\n".join(lines[-3:])
            )
            m["content"] = snipped
            changed = True
        return changed

    @staticmethod
    def _is_real_user_turn_start(message: dict) -> bool:
        if message.get("role") != "user":
            return False
        if is_internal_visual_context(message.get("content")):
            return False
        content = content_text(message.get("content", "")).strip()
        return not content.startswith(("[Context compressed - conversation summary]", "[Hard context reset]"))

    def _split_by_recent_turns(
        self,
        messages: list[dict],
        keep_recent_turns: int,
        fallback_keep_messages: int | None = None,
    ) -> tuple[list[dict], list[dict]]:
        user_indices = [i for i, message in enumerate(messages) if self._is_real_user_turn_start(message)]
        if keep_recent_turns > 0 and len(user_indices) > keep_recent_turns:
            tail_start = user_indices[-keep_recent_turns]
            return messages[:tail_start], messages[tail_start:]
        if fallback_keep_messages is not None and len(messages) > fallback_keep_messages:
            return messages[:-fallback_keep_messages], messages[-fallback_keep_messages:]
        return [], list(messages)

    def _summarize_old(self, messages: list[dict], llm: LLM | None,
                       keep_recent: int = 2) -> bool:
        """Layer 2: Summarize old conversation, keep recent user turns intact."""
        old, tail = self._split_by_recent_turns(messages, keep_recent_turns=keep_recent)
        if not old:
            return False

        summary = self._get_summary(old, llm)

        messages.clear()
        messages.append({
            "role": "user",
            "content": f"[Context compressed - conversation summary]\n{summary}",
        })
        messages.append({
            "role": "assistant",
            "content": "Got it, I have the context from our earlier conversation.",
        })
        messages.extend(tail)
        return True

    def _hard_collapse(self, messages: list[dict], llm: LLM | None):
        """Layer 3: Emergency compression. Prefer whole recent turns; fall back if needed."""
        old, tail = self._split_by_recent_turns(
            messages,
            keep_recent_turns=self._collapse_keep_recent,
            fallback_keep_messages=max(2, self._collapse_keep_recent * 4),
        )
        if not old:
            return
        summary = self._get_summary(old, llm)

        messages.clear()
        messages.append({
            "role": "user",
            "content": f"[Hard context reset]\n{summary}",
        })
        messages.append({
            "role": "assistant",
            "content": "Context restored. Continuing from where we left off.",
        })
        messages.extend(tail)

    def _get_summary(self, messages: list[dict], llm: LLM | None) -> str:
        """Generate summary via LLM or fallback to extraction."""
        flat = self._flatten(messages)

        if llm:
            try:
                resp = llm.chat(
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Compress this conversation into a brief summary. "
                                "Preserve: file paths edited, key decisions made, "
                                "errors encountered, current task state. "
                                "Drop: verbose command output, code listings, "
                                "redundant back-and-forth."
                            ),
                        },
                        {"role": "user", "content": flat[:self._summary_input_chars]},
                    ],
                )
                return resp.content
            except Exception:
                pass

        # fallback: extract key lines
        return self._extract_key_info(messages)

    @staticmethod
    def _flatten(messages: list[dict]) -> str:
        parts = []
        for m in messages:
            role = m.get("role", "?")
            text = content_text(m.get("content", ""))
            if text:
                parts.append(f"[{role}] {text[:400]}")
        return "\n".join(parts)

    @staticmethod
    def _extract_key_info(messages: list[dict]) -> str:
        """Fallback: extract file paths, errors, and decisions without LLM."""
        import re
        files_seen = set()
        errors = []
        decisions = []

        for m in messages:
            text = content_text(m.get("content", ""))
            # extract file paths
            for match in re.finditer(r'[\w./\-]+\.\w{1,5}', text):
                files_seen.add(match.group())
            # extract error lines
            for line in text.splitlines():
                if 'error' in line.lower() or 'Error' in line:
                    errors.append(line.strip()[:150])

        parts = []
        if files_seen:
            parts.append(f"Files touched: {', '.join(sorted(files_seen)[:20])}")
        if errors:
            parts.append(f"Errors seen: {'; '.join(errors[:5])}")
        return "\n".join(parts) or "(no extractable context)"

