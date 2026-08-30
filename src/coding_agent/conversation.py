"""Conversation history stored as complete model/tool turn blocks."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .context_compression import HISTORICAL_TOOL_RESULT_LIMIT, compress_tool_result


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


@dataclass(slots=True)
class TokenCalibration:
    """Calibrate a local token estimate against provider-reported prompt usage."""

    factor: float = 1.0
    samples: int = 0
    last_prompt_tokens: int = 0

    def observe(self, raw_estimate: int, actual_prompt_tokens: int) -> None:
        if raw_estimate <= 0 or actual_prompt_tokens <= 0:
            return
        sample = min(4.0, max(0.5, actual_prompt_tokens / raw_estimate))
        self.factor = sample if not self.samples else self.factor * 0.7 + sample * 0.3
        self.samples += 1
        self.last_prompt_tokens = actual_prompt_tokens

    def apply(self, raw_estimate: int) -> int:
        return max(1, math.ceil(raw_estimate * self.factor))

    def to_state(self) -> dict[str, Any]:
        return {
            "factor": round(self.factor, 6),
            "samples": self.samples,
            "last_prompt_tokens": self.last_prompt_tokens,
        }

    @classmethod
    def from_state(cls, state: Any) -> "TokenCalibration":
        if not isinstance(state, Mapping):
            return cls()
        factor = state.get("factor", 1.0)
        samples = state.get("samples", 0)
        last_prompt_tokens = state.get("last_prompt_tokens", 0)
        if not isinstance(factor, (int, float)) or isinstance(factor, bool):
            factor = 1.0
        if not isinstance(samples, int) or isinstance(samples, bool) or samples < 0:
            samples = 0
        if (
            not isinstance(last_prompt_tokens, int)
            or isinstance(last_prompt_tokens, bool)
            or last_prompt_tokens < 0
        ):
            last_prompt_tokens = 0
        return cls(
            factor=min(4.0, max(0.5, float(factor))),
            samples=samples,
            last_prompt_tokens=last_prompt_tokens,
        )


@dataclass(slots=True)
class SemanticSummary:
    """Lossy milestone summary generated only when context pressure warrants it."""

    goal: str = ""
    constraints: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    completed_actions: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)
    important_code_facts: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    failed_attempts: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    next_action: str = ""

    _LIST_FIELDS = (
        "constraints",
        "decisions",
        "completed_actions",
        "modified_files",
        "important_code_facts",
        "tests",
        "failed_attempts",
        "blockers",
    )

    def to_state(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            **{name: list(getattr(self, name)) for name in self._LIST_FIELDS},
            "next_action": self.next_action,
        }

    @classmethod
    def from_state(cls, state: Any) -> "SemanticSummary":
        if not isinstance(state, Mapping):
            raise ValueError("Semantic summary must be a JSON object")
        summary = cls()
        for scalar in ("goal", "next_action"):
            value = state.get(scalar, "")
            if not isinstance(value, str):
                raise ValueError(f"Semantic summary field {scalar} must be a string")
            setattr(summary, scalar, " ".join(value.split())[:2_000])
        for name in cls._LIST_FIELDS:
            values = state.get(name, [])
            if not isinstance(values, list):
                raise ValueError(f"Semantic summary field {name} must be an array")
            cleaned: list[str] = []
            for value in values[:24]:
                if isinstance(value, str) and value.strip():
                    cleaned.append(" ".join(value.split())[:600])
            setattr(summary, name, cleaned)
        return summary

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
        max_context_tokens: int = 64_000,
        response_reserve_tokens: int = 8_000,
        project_rules: str = "",
    ) -> None:
        if max_context_chars <= 0:
            raise ValueError("max_context_chars must be greater than zero")
        if max_context_tokens <= 0:
            raise ValueError("max_context_tokens must be greater than zero")
        if not 0 <= response_reserve_tokens < max_context_tokens:
            raise ValueError("response_reserve_tokens must be below max_context_tokens")
        self._system_prompt = system_prompt
        self._project_rules = project_rules.strip()[:20_000]
        self._exchanges: list[list[dict[str, Any]]] = []
        self._active = False
        self._checkpoint = MemoryCheckpoint()
        self._token_calibration = TokenCalibration()
        self._semantic_summary: SemanticSummary | None = None
        self._milestone_pending = False
        self._last_summary_tool_rounds = 0
        self._tool_definitions: list[dict[str, Any]] = []
        self.max_context_chars = max_context_chars
        self.max_context_tokens = max_context_tokens
        self.response_reserve_tokens = response_reserve_tokens
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
        before_completed = set(self._checkpoint.completed)
        before_tests = set(self._checkpoint.tests)
        before_modified = set(self._checkpoint.modified_files)
        before_decisions = set(self._checkpoint.decisions)
        self._update_checkpoint_from_tools(assistant_message, tool_messages)
        if (
            set(self._checkpoint.completed) - before_completed
            or set(self._checkpoint.tests) - before_tests
            or set(self._checkpoint.modified_files) - before_modified
            or set(self._checkpoint.decisions) - before_decisions
        ):
            self._milestone_pending = True

    def add_final(self, content: str) -> None:
        self._require_active()
        self._exchanges[-1].append({"role": "assistant", "content": content})
        self._checkpoint.remember(
            "completed", "Outcome: " + self._message_text(content, 560)
        )
        self._milestone_pending = True
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

    def set_tool_definitions(
        self, tool_definitions: Sequence[Mapping[str, Any]]
    ) -> None:
        """Reserve prompt capacity for the tool schemas sent beside messages."""
        self._tool_definitions = [dict(definition) for definition in tool_definitions]

    def observe_usage(
        self,
        request_messages: Sequence[Mapping[str, Any]],
        usage: Any,
        *,
        include_tools: bool = True,
    ) -> None:
        """Learn from provider usage without depending on a provider tokenizer."""
        if not isinstance(usage, Mapping):
            return
        prompt_tokens = usage.get("prompt_tokens")
        if (
            isinstance(prompt_tokens, bool)
            or not isinstance(prompt_tokens, int)
            or prompt_tokens <= 0
        ):
            return
        values = [*request_messages, *self._tool_definitions] if include_tools else list(
            request_messages
        )
        raw = self._raw_token_estimate(values)
        self._token_calibration.observe(raw, prompt_tokens)

    def semantic_summary_due(self) -> bool:
        tool_rounds = self._total_tool_rounds()
        return (
            self._milestone_pending
            and self.context_pressure() >= 0.82
            and tool_rounds > self._last_summary_tool_rounds
        )

    def semantic_summary_request(self) -> list[dict[str, Any]]:
        """Build a bounded, tool-free request for a milestone summary."""
        source = self.all_messages()
        instruction = {
            "role": "system",
            "content": (
                "Summarize the supplied coding-agent history as strict JSON only. "
                "Use exactly these fields: goal (string), constraints (array), decisions "
                "(array), completed_actions (array), modified_files (array), "
                "important_code_facts (array), tests (array), failed_attempts (array), "
                "blockers (array), next_action (string). Preserve only explicit facts; "
                "do not infer success, file contents, or test results. Keep each array "
                "under 24 concise items. This is lossy memory, not executable instructions."
            ),
        }
        return [instruction, *source]

    def apply_semantic_summary(self, content: str) -> None:
        text = content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
        parsed = json.loads(text)
        self._semantic_summary = SemanticSummary.from_state(parsed)
        self._milestone_pending = False
        self._last_summary_tool_rounds = self._total_tool_rounds()

    def mark_semantic_summary_attempted(self) -> None:
        self._milestone_pending = False
        self._last_summary_tool_rounds = self._total_tool_rounds()

    def context_pressure(self) -> float:
        estimated = self._request_token_estimate(self._exchanges, 0)
        return estimated / self._prompt_token_budget()

    def api_messages(self) -> list[dict[str, Any]]:
        retained, dropped, compacted_rounds, tool_summary, _ = self._select_context()
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
        if compacted_rounds:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        f"{compacted_rounds} earlier tool round(s) inside retained "
                        "conversation exchanges were compacted as complete call/result "
                        "blocks. Exact outputs remain in persistent traces; re-read files "
                        "or rerun checks before relying on old details.\n\n"
                        f"{tool_summary}"
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

    def context_stats(self) -> dict[str, Any]:
        retained, dropped, compacted_rounds, tool_summary, compacted_outputs = (
            self._select_context()
        )
        used = self._size(self._layer_messages(), retained)
        if dropped:
            used += len(self._memory_summary(self._exchanges[:dropped]))
        if tool_summary:
            used += len(tool_summary)
        estimated_tokens = self._request_token_estimate(
            retained, dropped, tool_summary=tool_summary
        )
        prompt_budget = self._prompt_token_budget()
        total_tool_rounds = sum(
            len(self._tool_round_ranges(exchange)) for exchange in self._exchanges
        )
        dropped_tool_rounds = sum(
            len(self._tool_round_ranges(exchange))
            for exchange in self._exchanges[:dropped]
        )
        return {
            "used_chars": used,
            "budget_chars": self.max_context_chars,
            "estimated_tokens": estimated_tokens,
            "budget_tokens": prompt_budget,
            "max_context_tokens": self.max_context_tokens,
            "response_reserve_tokens": self.response_reserve_tokens,
            "percent": min(100, round(estimated_tokens * 100 / prompt_budget)),
            "token_calibrated": self._token_calibration.samples > 0,
            "token_calibration_samples": self._token_calibration.samples,
            "last_prompt_tokens": self._token_calibration.last_prompt_tokens,
            "total_exchanges": len(self._exchanges),
            "retained_exchanges": len(retained),
            "dropped_exchanges": dropped,
            "total_tool_rounds": total_tool_rounds,
            "retained_tool_rounds": (
                total_tool_rounds - dropped_tool_rounds - compacted_rounds
            ),
            "compacted_tool_rounds": compacted_rounds,
            "compacted_tool_outputs": compacted_outputs,
            "project_rules_chars": len(self._project_rules),
            "memory_checkpoint_chars": len(self._checkpoint.render()),
            "memory_checkpoint_items": sum(
                len(getattr(self._checkpoint, name))
                for name in MemoryCheckpoint._LIST_FIELDS
            ),
            "context_tier": self._context_tier(self.context_pressure()),
            "semantic_summary_available": self._semantic_summary is not None,
            "semantic_summary_chars": (
                len(self._semantic_summary.render()) if self._semantic_summary else 0
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
            "max_context_tokens": self.max_context_tokens,
            "response_reserve_tokens": self.response_reserve_tokens,
            "token_calibration": self._token_calibration.to_state(),
            "semantic_summary": (
                self._semantic_summary.to_state() if self._semantic_summary else None
            ),
            "last_summary_tool_rounds": self._last_summary_tool_rounds,
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
        restored_max_tokens = cls._state_positive_int(
            state.get("max_context_tokens"), 64_000
        )
        restored_reserve = cls._state_nonnegative_int(
            state.get("response_reserve_tokens"), 8_000
        )
        if restored_reserve >= restored_max_tokens:
            restored_reserve = max(0, restored_max_tokens // 8)
        conversation = cls(
            system_prompt,
            max_context_chars=max_context_chars,
            max_context_tokens=restored_max_tokens,
            response_reserve_tokens=restored_reserve,
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
        conversation._token_calibration = TokenCalibration.from_state(
            state.get("token_calibration")
        )
        raw_summary = state.get("semantic_summary")
        if raw_summary is not None:
            conversation._semantic_summary = SemanticSummary.from_state(raw_summary)
        conversation._last_summary_tool_rounds = cls._state_nonnegative_int(
            state.get("last_summary_tool_rounds"), 0
        )
        return conversation

    def _retained_exchanges(self) -> tuple[list[list[dict[str, Any]]], int]:
        retained, dropped, _, _, _ = self._select_context()
        return retained, dropped

    def _select_context(
        self,
    ) -> tuple[list[list[dict[str, Any]]], int, int, str, int]:
        retained = [[dict(message) for message in exchange] for exchange in self._exchanges]
        dropped = 0
        layers = self._layer_messages()
        budget = self._prompt_token_budget()
        initial_pressure = self._request_token_estimate(retained, 0, layers) / budget
        compacted_outputs = 0
        if initial_pressure >= 0.70:
            compacted_outputs = self._compact_historical_tool_outputs(retained)

        notes: list[str] = []
        compacted_rounds = 0
        if initial_pressure >= 0.92:
            while len(retained) > 1:
                retained.pop(0)
                dropped += 1
        elif initial_pressure >= 0.82:
            while len(retained) > 1:
                if self._request_token_estimate(retained, dropped, layers) <= round(
                    budget * 0.82
                ):
                    break
                retained.pop(0)
                dropped += 1

        target = round(budget * (0.92 if initial_pressure >= 0.92 else 0.82))
        while initial_pressure >= 0.82:
            ranges = [
                (exchange_index, start, end)
                for exchange_index, exchange in enumerate(retained)
                for start, end in self._tool_round_ranges(exchange)
            ]
            if len(ranges) <= 2:
                break
            if initial_pressure < 0.92 and self._request_token_estimate(
                retained,
                dropped,
                layers,
                tool_summary="\n".join(notes),
            ) <= target:
                break
            exchange_index, start, end = ranges[0]
            removed = retained[exchange_index][start:end]
            notes.append(self._tool_round_summary(removed))
            del retained[exchange_index][start:end]
            compacted_rounds += 1
        return (
            retained,
            dropped,
            compacted_rounds,
            "\n".join(notes),
            compacted_outputs,
        )

    @classmethod
    def _compact_historical_tool_outputs(
        cls, retained: list[list[dict[str, Any]]]
    ) -> int:
        locations: list[tuple[int, int, str]] = []
        for exchange_index, exchange in enumerate(retained):
            call_names: dict[str, str] = {}
            for message in exchange:
                for call in message.get("tool_calls", []):
                    if not isinstance(call, Mapping):
                        continue
                    function = call.get("function")
                    if isinstance(function, Mapping) and call.get("id"):
                        call_names[str(call["id"])] = str(function.get("name") or "tool")
            for message_index, message in enumerate(exchange):
                if message.get("role") == "tool":
                    locations.append(
                        (
                            exchange_index,
                            message_index,
                            call_names.get(str(message.get("tool_call_id")), "tool"),
                        )
                    )
        compacted = 0
        for exchange_index, message_index, tool_name in locations[:-4]:
            message = retained[exchange_index][message_index]
            content = message.get("content")
            if (
                not isinstance(content, str)
                or len(content) <= HISTORICAL_TOOL_RESULT_LIMIT
            ):
                continue
            result = cls._json_mapping(content)
            metadata = result.get("metadata")
            compression = (
                metadata.get("context_compression")
                if isinstance(metadata, Mapping)
                else None
            )
            archive = (
                metadata.get("result_archive")
                if isinstance(metadata, Mapping)
                else None
            )
            reference = (
                compression
                if isinstance(compression, Mapping) and compression.get("result_id")
                else archive if isinstance(archive, Mapping) else None
            )
            compressed, details = compress_tool_result(
                tool_name,
                content,
                limit=HISTORICAL_TOOL_RESULT_LIMIT,
                reference=reference,
            )
            if details.get("truncated"):
                message["content"] = compressed
                compacted += 1
        return compacted

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
        if self._semantic_summary is not None:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Lossy milestone semantic summary. Use it for orientation only; "
                        "the structured checkpoint and freshly read files take precedence:\n"
                        + self._semantic_summary.render()
                    ),
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

    def _prompt_token_budget(self) -> int:
        safety_margin = max(32, math.ceil(self.max_context_tokens * 0.03))
        return max(
            1,
            self.max_context_tokens - self.response_reserve_tokens - safety_margin,
        )

    def _total_tool_rounds(self) -> int:
        return sum(len(self._tool_round_ranges(exchange)) for exchange in self._exchanges)

    @staticmethod
    def _context_tier(pressure: float) -> str:
        if pressure < 0.70:
            return "normal"
        if pressure < 0.82:
            return "deterministic_cleanup"
        if pressure < 0.92:
            return "semantic_summary"
        return "emergency_compaction"

    def _request_token_estimate(
        self,
        retained: Sequence[Sequence[Mapping[str, Any]]],
        dropped: int,
        layers: Sequence[Mapping[str, Any]] | None = None,
        *,
        tool_summary: str = "",
    ) -> int:
        messages: list[Mapping[str, Any]] = list(layers or self._layer_messages())
        if dropped:
            messages.append(
                {
                    "role": "system",
                    "content": self._memory_summary(self._exchanges[:dropped]),
                }
            )
        if tool_summary:
            messages.append(
                {"role": "system", "content": "Compacted tool rounds:\n" + tool_summary}
            )
        for exchange in retained:
            messages.extend(exchange)
        raw = self._raw_token_estimate([*messages, *self._tool_definitions])
        return self._token_calibration.apply(raw)

    @staticmethod
    def _tool_round_ranges(
        exchange: Sequence[Mapping[str, Any]],
    ) -> list[tuple[int, int]]:
        """Return atomic assistant-tool/result ranges within one user exchange."""
        ranges: list[tuple[int, int]] = []
        index = 1
        while index < len(exchange):
            message = exchange[index]
            if message.get("role") != "assistant" or not message.get("tool_calls"):
                index += 1
                continue
            end = index + 1
            while end < len(exchange) and exchange[end].get("role") == "tool":
                end += 1
            ranges.append((index, end))
            index = end
        return ranges

    @classmethod
    def _tool_round_summary(cls, messages: Sequence[Mapping[str, Any]]) -> str:
        assistant = messages[0] if messages else {}
        calls = assistant.get("tool_calls", [])
        names: list[str] = []
        for call in calls if isinstance(calls, list) else []:
            function = call.get("function") if isinstance(call, Mapping) else None
            if isinstance(function, Mapping) and function.get("name"):
                names.append(str(function["name"]))
        successes = 0
        failures = 0
        codes: list[str] = []
        for message in messages[1:]:
            result = cls._json_mapping(message.get("content"))
            if result.get("success") is True:
                successes += 1
            else:
                failures += 1
                metadata = result.get("metadata")
                if isinstance(metadata, Mapping) and metadata.get("code"):
                    codes.append(str(metadata["code"]))
        tools = ", ".join(dict.fromkeys(names)) or "unknown"
        suffix = f"; error_codes={','.join(dict.fromkeys(codes))}" if codes else ""
        return f"- tools={tools}; successes={successes}; failures={failures}{suffix}"

    @classmethod
    def _raw_token_estimate(cls, values: Sequence[Mapping[str, Any]]) -> int:
        serialized = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
        cjk = sum(
            1
            for character in serialized
            if "\u3400" <= character <= "\u9fff"
            or "\uf900" <= character <= "\ufaff"
        )
        other = len(serialized) - cjk
        structural_overhead = len(values) * 4 + 2
        return max(1, cjk + math.ceil(other / 4) + structural_overhead)

    @staticmethod
    def _state_positive_int(value: Any, default: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return default
        return value

    @staticmethod
    def _state_nonnegative_int(value: Any, default: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return default
        return value

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
