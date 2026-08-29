"""Conversation history stored as complete model/tool turn blocks."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


@dataclass(slots=True)
class MemoryCheckpoint:
    """Bounded, structured task memory kept independently of chat history."""

    goal: str = ""
    constraints: list[str] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    failed_attempts: list[str] = field(default_factory=list)
    do_not_repeat: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)

    _LIST_FIELDS = (
        "constraints",
        "completed",
        "decisions",
        "modified_files",
        "tests",
        "failed_attempts",
        "do_not_repeat",
        "next_steps",
    )
    _MAX_ITEMS = 24
    _MAX_ITEM_CHARS = 600

    def remember(self, field_name: str, value: Any) -> None:
        if field_name not in self._LIST_FIELDS:
            raise ValueError(f"Unknown memory field: {field_name}")
        text = " ".join(str(value or "").split())
        if not text:
            return
        text = text[: self._MAX_ITEM_CHARS]
        items: list[str] = getattr(self, field_name)
        if text in items:
            return
        items.append(text)
        if len(items) > self._MAX_ITEMS:
            del items[: len(items) - self._MAX_ITEMS]

    def replace(self, field_name: str, values: Sequence[Any]) -> None:
        if field_name not in self._LIST_FIELDS:
            raise ValueError(f"Unknown memory field: {field_name}")
        setattr(self, field_name, [])
        for value in values:
            self.remember(field_name, value)

    def to_state(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            **{name: list(getattr(self, name)) for name in self._LIST_FIELDS},
        }

    @classmethod
    def from_state(cls, state: Any) -> "MemoryCheckpoint":
        checkpoint = cls()
        if not isinstance(state, Mapping):
            return checkpoint
        goal = state.get("goal", "")
        if isinstance(goal, str):
            checkpoint.goal = goal[:2_000]
        for field_name in cls._LIST_FIELDS:
            values = state.get(field_name, [])
            if isinstance(values, list):
                checkpoint.replace(field_name, values)
        return checkpoint

    def render(self) -> str:
        return json.dumps(self.to_state(), ensure_ascii=False, indent=2)


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
        self._checkpoint = MemoryCheckpoint()
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
        self._checkpoint.goal = self._message_text(content, 2_000)
        for constraint in self._extract_constraints(content):
            self._checkpoint.remember("constraints", constraint)
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
        self._update_checkpoint_from_tools(assistant_message, tool_messages)

    def add_final(self, content: str) -> None:
        self._require_active()
        self._exchanges[-1].append({"role": "assistant", "content": content})
        self._checkpoint.remember(
            "completed", "Outcome: " + self._message_text(content, 560)
        )
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
            "memory_checkpoint_chars": len(self._checkpoint.render()),
            "memory_checkpoint_items": sum(
                len(getattr(self._checkpoint, name))
                for name in MemoryCheckpoint._LIST_FIELDS
            ),
        }

    def memory_checkpoint(self) -> dict[str, Any]:
        """Return a detached public snapshot for persistence and user interfaces."""
        return self._checkpoint.to_state()

    def to_state(self) -> dict[str, Any]:
        return {
            "system_prompt": self._system_prompt,
            "project_rules": self._project_rules,
            "max_context_chars": self.max_context_chars,
            "exchanges": self._exchanges,
            "active": self._active,
            "memory_checkpoint": self._checkpoint.to_state(),
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
        conversation._checkpoint = MemoryCheckpoint.from_state(
            state.get("memory_checkpoint")
        )
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
        checkpoint = self._checkpoint.to_state()
        if checkpoint["goal"] or any(
            checkpoint[name] for name in MemoryCheckpoint._LIST_FIELDS
        ):
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Structured task memory checkpoint. Treat it as a factual, "
                        "persistent status record; verify file details before editing:\n"
                        + self._checkpoint.render()
                    ),
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

    @classmethod
    def _extract_constraints(cls, content: str) -> list[str]:
        markers = re.compile(
            r"(必须|不要|不能|不许|只允许|需要|每次|务必|禁止|must\b|should\b|"
            r"do not\b|don't\b|never\b|only\b)",
            re.IGNORECASE,
        )
        parts = re.split(r"[\n。！？!?；;]+", content)
        return [cls._message_text(part, 500) for part in parts if markers.search(part)][:8]

    def _update_checkpoint_from_tools(
        self,
        assistant_message: Mapping[str, Any],
        tool_messages: Sequence[Mapping[str, Any]],
    ) -> None:
        calls = {
            str(call.get("id")): call
            for call in assistant_message.get("tool_calls", [])
            if isinstance(call, Mapping) and call.get("id")
        }
        for message in tool_messages:
            call = calls.get(str(message.get("tool_call_id")))
            if not call:
                continue
            function = call.get("function")
            if not isinstance(function, Mapping):
                continue
            name = str(function.get("name") or "unknown_tool")
            arguments = self._json_mapping(function.get("arguments"))
            result = self._json_mapping(message.get("content"))
            success = result.get("success") is True
            if name == "run_command":
                argv = arguments.get("argv")
                if isinstance(argv, list) and self._looks_like_test(argv):
                    command = " ".join(str(part) for part in argv)
                    metadata = result.get("metadata")
                    exit_code = (
                        metadata.get("exit_code")
                        if isinstance(metadata, Mapping)
                        else None
                    )
                    status = "passed" if success else "failed"
                    self._checkpoint.remember(
                        "tests", f"{command} ({status}, exit_code={exit_code})"
                    )
            if not success:
                error = self._message_text(result.get("error"), 400) or "unknown error"
                failure = f"{name}: {error}"
                self._checkpoint.remember("failed_attempts", failure)
                metadata = result.get("metadata")
                code = metadata.get("code") if isinstance(metadata, Mapping) else None
                if code in {"Conflict", "RepeatedToolCall", "ApprovalRejected"}:
                    self._checkpoint.remember("do_not_repeat", failure)
                continue

            if name in {"write_file", "replace_in_file"}:
                self._remember_modified_path(arguments.get("path"))
                self._checkpoint.remember(
                    "decisions", f"Applied {name} to {arguments.get('path')}"
                )
            elif name == "multi_edit":
                edits = arguments.get("edits")
                if isinstance(edits, list):
                    for edit in edits:
                        if isinstance(edit, Mapping):
                            self._remember_modified_path(edit.get("path"))
                self._checkpoint.remember(
                    "decisions", "Applied one atomic multi-file edit"
                )
            elif name == "update_plan":
                data = result.get("data")
                if isinstance(data, Mapping) and data.get("explanation"):
                    self._checkpoint.remember("decisions", data.get("explanation"))
                plan = data.get("plan") if isinstance(data, Mapping) else None
                if isinstance(plan, list):
                    completed: list[str] = []
                    next_steps: list[str] = []
                    for item in plan:
                        if not isinstance(item, Mapping):
                            continue
                        step = self._message_text(item.get("step"), 500)
                        if not step:
                            continue
                        if item.get("status") == "completed":
                            completed.append(step)
                        else:
                            next_steps.append(step)
                    self._checkpoint.replace("completed", completed)
                    self._checkpoint.replace("next_steps", next_steps)

    def _remember_modified_path(self, path: Any) -> None:
        if isinstance(path, str) and path.strip():
            self._checkpoint.remember("modified_files", path.strip())

    @staticmethod
    def _json_mapping(value: Any) -> Mapping[str, Any]:
        if isinstance(value, Mapping):
            return value
        if not isinstance(value, str):
            return {}
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, Mapping) else {}

    @staticmethod
    def _looks_like_test(argv: Sequence[Any]) -> bool:
        command = " ".join(str(part).lower() for part in argv)
        return any(
            marker in command
            for marker in ("pytest", "unittest", "npm test", "npm run test", "cargo test", "go test")
        )

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
