"""Conversation history stored as complete model/tool turn blocks."""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence


class Conversation:
    """Keep tool-call/result pairs intact while enforcing a simple context budget."""

    def __init__(
        self,
        system_prompt: str,
        user_task: str,
        *,
        max_context_chars: int = 120_000,
    ) -> None:
        self._prefix: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_task},
        ]
        self._turns: list[list[dict[str, Any]]] = []
        self.max_context_chars = max_context_chars

    def add_tool_turn(
        self,
        assistant_message: Mapping[str, Any],
        tool_messages: Sequence[Mapping[str, Any]],
    ) -> None:
        self._turns.append(
            [dict(assistant_message), *(dict(message) for message in tool_messages)]
        )

    def add_final(self, content: str) -> None:
        self._turns.append([{"role": "assistant", "content": content}])

    def api_messages(self) -> list[dict[str, Any]]:
        retained = list(self._turns)
        dropped = 0
        while len(retained) > 1 and self._size(self._prefix, retained) > self.max_context_chars:
            retained.pop(0)
            dropped += 1

        messages = list(self._prefix)
        if dropped:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        f"{dropped} earlier tool interaction block(s) were omitted to fit "
                        "the local context budget. Re-read files when exact state is needed."
                    ),
                }
            )
        for turn in retained:
            messages.extend(turn)
        return messages

    def all_messages(self) -> list[dict[str, Any]]:
        messages = list(self._prefix)
        for turn in self._turns:
            messages.extend(turn)
        return messages

    @staticmethod
    def _size(prefix: Sequence[Mapping[str, Any]], turns: Sequence[Sequence[Mapping[str, Any]]]) -> int:
        return sum(len(json.dumps(item, ensure_ascii=False)) for item in prefix) + sum(
            len(json.dumps(item, ensure_ascii=False))
            for turn in turns
            for item in turn
        )
