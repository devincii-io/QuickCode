"""Conversation history: message accumulation and cache breakpoints.

Holds the wire-neutral ``ChatMessage`` list. The request builder places the
cache breakpoint on the system tail and the last history block so the prefix
stays cache-stable across turns. Which is why nothing here rewrites an earlier
message to save tokens -- not even a file read that a later read superseded:
the cached prefix runs through it, and changing it is a cache miss for
everything after it. The history is rewritten only where the cache is lost
anyway (a compaction, a new system prompt, the context guard's cuts).
"""

from __future__ import annotations

from quickcode.core.events import AssembledToolCall, AssistantMessage
from quickcode.providers.base import ChatMessage


class History:
    def __init__(self, system_prompt: str) -> None:
        self._system = ChatMessage(role="system", content=system_prompt, cache_control=True)
        self.messages: list[ChatMessage] = []

    @property
    def system_prompt(self) -> str:
        return self._system.content

    def set_system_prompt(self, system_prompt: str) -> None:
        """Replace the system prompt (e.g. to refresh the model identity after a
        mid-conversation model switch, which already invalidates the cache)."""
        if system_prompt != self._system.content:
            self._drop_reasoning()
        self._system = ChatMessage(role="system", content=system_prompt, cache_control=True)

    def _drop_reasoning(self) -> None:
        """Forget signed reasoning once the history before it is rewritten.

        A provider signature binds each thinking block to the exact prefix that
        produced it; after a new system prompt or a compaction the API refuses
        those blocks, and every later request would pay for that refusal. The
        rewrite is the boundary where letting them go costs least.
        """
        for m in self.messages:
            m.reasoning_blocks = []

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
            ChatMessage(
                role="assistant",
                content=msg.text,
                tool_calls=tool_calls,
                reasoning_blocks=list(msg.reasoning_blocks),
            )
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
        for m in self.messages:
            m.cache_control = False
        if self.messages:
            self.messages[-1].cache_control = True
        return [self._system, *self.messages]

    def replace_with_summary(self, summary: str, tail: list[ChatMessage]) -> None:
        """Post-compaction rebuild: seed message + verbatim tail."""
        self.messages = [
            ChatMessage(role="user", content=f"<compaction-summary>{summary}</compaction-summary>"),
            *tail,
        ]
        self._drop_reasoning()
