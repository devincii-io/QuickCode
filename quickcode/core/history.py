"""Conversation history: message accumulation, read-dedup, cache breakpoints.

Holds the wire-neutral ``ChatMessage`` list. The request builder places the
cache breakpoint on the system tail and the last history block so the prefix
stays cache-stable across turns.
"""

from __future__ import annotations

from quickcode.core.events import AssembledToolCall, AssistantMessage
from quickcode.providers.base import ChatMessage


class History:
    def __init__(self, system_prompt: str) -> None:
        self._system = ChatMessage(role="system", content=system_prompt, cache_control=True)
        self.messages: list[ChatMessage] = []

    # ---- appenders ----
    def push_user(self, text: str, reminders: list[str] | None = None) -> None:
        content = text
        if reminders:
            content = (content + "\n\n" + "\n".join(reminders)).strip()
        self.messages.append(ChatMessage(role="user", content=content))

    def push_assistant(self, msg: AssistantMessage) -> None:
        tool_calls = [
            {"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in msg.tool_calls
        ]
        self.messages.append(
            ChatMessage(role="assistant", content=msg.text, tool_calls=tool_calls)
        )

    def push_tool_results(self, results: list[tuple[AssembledToolCall, str, bool]]) -> None:
        """All tool results for a round go in as consecutive tool messages.

        Splitting them across turns trains the model out of parallel calls, so
        they are pushed together here in call order.
        """
        for call, content, is_error in results:
            body = content if not is_error else f"[error] {content}"
            self.messages.append(
                ChatMessage(role="tool", content=body, tool_call_id=call.id, name=call.name)
            )

    # ---- request assembly ----
    def build_messages(self) -> list[ChatMessage]:
        """system → history, with a cache breakpoint on the last block."""
        msgs = [self._system, *self.messages]
        if self.messages:
            self.messages[-1].cache_control = True
            for m in self.messages[:-1]:
                m.cache_control = False
        return msgs

    def replace_with_summary(self, summary: str, tail: list[ChatMessage]) -> None:
        """Post-compaction rebuild: seed message + verbatim tail."""
        self.messages = [
            ChatMessage(role="user", content=f"<compaction-summary>{summary}</compaction-summary>"),
            *tail,
        ]
