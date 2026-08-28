"""Conversation history stored as complete model/tool turn blocks."""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence


class Conversation:
    """Store complete user exchanges while enforcing a simple context budget."""

    def __init__(
        self,
        system_prompt: str,
        user_task: str | None = None,
        *,
        max_context_chars: int = 120_000,
        project_rules: str = "",
    ) -> None:
        if max_context_chars <= 0:
            raise ValueError("max_context_chars must be greater than zero")
        self._system_prompt = system_prompt
        self._project_rules = project_rules.strip()[:20_000]
        self._exchanges: list[list[dict[str, Any]]] = []
        self._active = False
        self.max_context_chars = max_context_chars
        if user_task is not None:
            self.start_user_turn(user_task)

    def start_user_turn(self, content: str) -> None:
        if self._active:
            raise ValueError("Cannot start a new user turn before the current turn finishes")
        content = content.strip()
        if not content:
            raise ValueError("User message must not be empty")
        self._exchanges.append([{"role": "user", "content": content}])
        self._active = True

    def add_tool_turn(
        self,
        assistant_message: Mapping[str, Any],
        tool_messages: Sequence[Mapping[str, Any]],
    ) -> None:
        self._require_active()
        self._exchanges[-1].extend(
            [dict(assistant_message), *(dict(message) for message in tool_messages)]
        )

    def add_final(self, content: str) -> None:
        self._require_active()
        self._exchanges[-1].append({"role": "assistant", "content": content})
        self._active = False

    def abort_turn(self, content: str) -> None:
        """Close an interrupted exchange so a later user turn remains well-formed."""
        if not self._active:
            return
        self._exchanges[-1].append({"role": "assistant", "content": content})
        self._active = False

    @property
    def has_active_turn(self) -> bool:
        return self._active

    def set_system_prompt(self, system_prompt: str) -> None:
        """Refresh stable runtime rules when a persisted session is restored."""
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise ValueError("System prompt must not be empty")
        self._system_prompt = system_prompt

    def api_messages(self) -> list[dict[str, Any]]:
        retained, dropped = self._retained_exchanges()
        messages = self._layer_messages()
        if dropped:
            memory = self._memory_summary(self._exchanges[:dropped])
            messages.append(
                {
                    "role": "system",
                    "content": (
                        f"{dropped} earlier conversation exchange(s) were compacted locally. "
                        "The user requests and final outcomes are summarized below; exact "
                        "tool traces were omitted. Re-read files when exact state is needed.\n\n"
                        f"{memory}"
                    ),
                }
            )
        for exchange in retained:
            messages.extend(exchange)
        return messages

    def all_messages(self) -> list[dict[str, Any]]:
        messages = self._layer_messages()
        for exchange in self._exchanges:
            messages.extend(exchange)
        return messages

    def context_stats(self) -> dict[str, int]:
        retained, dropped = self._retained_exchanges()
        used = self._size(self._layer_messages(), retained)
        if dropped:
            used += len(self._memory_summary(self._exchanges[:dropped]))
        return {
            "used_chars": used,
            "budget_chars": self.max_context_chars,
            "percent": min(100, round(used * 100 / self.max_context_chars)),
            "total_exchanges": len(self._exchanges),
            "retained_exchanges": len(retained),
            "dropped_exchanges": dropped,
            "project_rules_chars": len(self._project_rules),
        }

    def to_state(self) -> dict[str, Any]:
        return {
            "system_prompt": self._system_prompt,
            "project_rules": self._project_rules,
            "max_context_chars": self.max_context_chars,
            "exchanges": self._exchanges,
            "active": self._active,
        }

    @classmethod
    def from_state(cls, state: Mapping[str, Any]) -> "Conversation":
        system_prompt = state.get("system_prompt")
        max_context_chars = state.get("max_context_chars")
        exchanges = state.get("exchanges")
        active = state.get("active", False)
        if not isinstance(system_prompt, str) or not system_prompt:
            raise ValueError("Conversation state has no valid system prompt")
        if isinstance(max_context_chars, bool) or not isinstance(max_context_chars, int):
            raise ValueError("Conversation state has an invalid context budget")
        if not isinstance(exchanges, list):
            raise ValueError("Conversation state has invalid exchanges")

        project_rules = state.get("project_rules", "")
        if not isinstance(project_rules, str):
            raise ValueError("Conversation state has invalid project rules")
        conversation = cls(
            system_prompt,
            max_context_chars=max_context_chars,
            project_rules=project_rules,
        )
        normalized: list[list[dict[str, Any]]] = []
        for exchange in exchanges:
            if not isinstance(exchange, list) or not exchange:
                raise ValueError("Conversation state contains an invalid exchange")
            normalized_exchange: list[dict[str, Any]] = []
            for message in exchange:
                if not isinstance(message, Mapping) or not isinstance(
                    message.get("role"), str
                ):
                    raise ValueError("Conversation state contains an invalid message")
                normalized_exchange.append(dict(message))
            if normalized_exchange[0].get("role") != "user":
                raise ValueError("Every conversation exchange must start with a user")
            normalized.append(normalized_exchange)
        conversation._exchanges = normalized
        conversation._active = bool(active)
        return conversation

    def _retained_exchanges(self) -> tuple[list[list[dict[str, Any]]], int]:
        retained = list(self._exchanges)
        dropped = 0
        layers = self._layer_messages()
        while len(retained) > 1:
            memory_size = (
                len(self._memory_summary(self._exchanges[:dropped])) if dropped else 0
            )
            if (
                self._size(layers, retained) + memory_size
                <= self.max_context_chars
            ):
                break
            retained.pop(0)
            dropped += 1
        return retained, dropped

    def _layer_messages(self) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt}
        ]
        if self._project_rules:
            messages.append(
                {
                    "role": "system",
                    "content": "Project rules (higher priority than task preferences):\n"
                    + self._project_rules,
                }
            )
        summary = self._completed_task_summary()
        if summary:
            messages.append(
                {
                    "role": "system",
                    "content": "Task history summary:\n" + summary,
                }
            )
        if self._active and self._exchanges:
            current = self._message_text(self._exchanges[-1][0].get("content"), 2_000)
            messages.append(
                {
                    "role": "system",
                    "content": "Current TODO (newest user request):\n- " + current,
                }
            )
        return messages

    def _completed_task_summary(self) -> str:
        completed = self._exchanges[:-1] if self._active else self._exchanges
        if not completed:
            return ""
        return self._memory_summary(completed[-8:])[:6_000]

    @classmethod
    def _memory_summary(cls, exchanges: Sequence[Sequence[Mapping[str, Any]]]) -> str:
        """Create a bounded factual digest without another model call."""
        summaries: list[str] = []
        used = 0
        for exchange in reversed(exchanges):
            user_content = cls._message_text(exchange[0].get("content"), 420)
            final_content = ""
            for message in reversed(exchange):
                if message.get("role") == "assistant" and not message.get("tool_calls"):
                    final_content = cls._message_text(message.get("content"), 680)
                    break
            summary = f"- User request: {user_content}"
            if final_content:
                summary += f"\n  Assistant outcome: {final_content}"
            if used + len(summary) > 6_000:
                break
            summaries.append(summary)
            used += len(summary)
        summaries.reverse()
        return "\n".join(summaries) or "- Earlier exchanges contained no text summary."

    @staticmethod
    def _message_text(value: Any, limit: int) -> str:
        text = " ".join(str(value or "").split())
        return text[:limit] + ("…" if len(text) > limit else "")

    def _require_active(self) -> None:
        if not self._active or not self._exchanges:
            raise ValueError("No active user turn")

    @staticmethod
    def _size(
        prefix: Sequence[Mapping[str, Any]],
        exchanges: Sequence[Sequence[Mapping[str, Any]]],
    ) -> int:
        return sum(
            len(json.dumps(item, ensure_ascii=False)) for item in prefix
        ) + sum(
            len(json.dumps(item, ensure_ascii=False))
            for exchange in exchanges
            for item in exchange
        )
