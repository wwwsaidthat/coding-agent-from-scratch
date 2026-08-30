"""The framework-free model/tool execution loop."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable, Mapping

from .conversation import Conversation
from .context_compression import HISTORICAL_TOOL_RESULT_LIMIT, compress_tool_result
from .models import ChatModel, ModelResponse, ToolCall
from .prompts import SYSTEM_PROMPT
from .tools.registry import ToolRegistry


EventHandler = Callable[[str, Mapping[str, Any]], None]
StopChecker = Callable[[], bool]


class AgentError(RuntimeError):
    """Base class for controlled agent failures."""


class AgentLimitError(AgentError):
    """Raised when the local step budget is exhausted."""


class AgentCancelledError(AgentError):
    """Raised when a caller requests cancellation between agent actions."""


@dataclass(frozen=True, slots=True)
class AgentResult:
    final_output: str
    steps: int
    tool_calls: int
    messages: tuple[Mapping[str, Any], ...]


class Agent:
    def __init__(
        self,
        model: ChatModel,
        tools: ToolRegistry,
        *,
        max_steps: int = 20,
        max_context_chars: int = 120_000,
        max_context_tokens: int = 192_000,
        on_event: EventHandler | None = None,
        should_stop: StopChecker | None = None,
    ) -> None:
        if max_steps <= 0:
            raise ValueError("max_steps must be greater than zero")
        self.model = model
        self.tools = tools
        self.max_steps = max_steps
        self.max_context_chars = max_context_chars
        self.max_context_tokens = max_context_tokens
        self.on_event = on_event
        self.should_stop = should_stop

    def run(self, task: str, *, conversation: Conversation | None = None) -> AgentResult:
        if not task.strip():
            raise ValueError("Task must not be empty")

        if conversation is None:
            conversation = Conversation(
                SYSTEM_PROMPT,
                max_context_chars=self.max_context_chars,
                max_context_tokens=self.max_context_tokens,
            )
        conversation.set_tool_definitions(self.tools.definitions)
        conversation.start_user_turn(task)
        tool_call_count = 0
        previous_fingerprint: tuple[str, str] | None = None
        repeated_calls = 0

        for step in range(1, self.max_steps + 1):
            self._check_cancelled()
            request_messages = conversation.api_messages()
            request_started = time.perf_counter()
            self._emit(
                "model_request",
                {
                    "step": step,
                    "message_count": len(request_messages),
                    "tool_definition_count": len(self.tools.definitions),
                    "request_messages": self._trace_messages(request_messages),
                    "tool_definitions": self.tools.definitions,
                },
            )
            try:
                response = self.model.complete(request_messages, self.tools.definitions)
            except Exception as exc:
                self._emit(
                    "model_error",
                    {
                        "step": step,
                        "duration_ms": round(
                            (time.perf_counter() - request_started) * 1_000, 2
                        ),
                        "error_type": type(exc).__name__,
                        "error_code": self._error_code(exc),
                        "message": str(exc),
                    },
                )
                raise
            duration_ms = round((time.perf_counter() - request_started) * 1_000, 2)
            conversation.observe_usage(request_messages, response.metadata.get("usage"))
            self._emit(
                "model_response",
                {
                    "step": step,
                    "tool_call_count": len(response.tool_calls),
                    "thought": self._public_decision_summary(response),
                    "duration_ms": duration_ms,
                    "model": response.metadata.get("model"),
                    "request_id": response.metadata.get("id"),
                    "usage": response.metadata.get("usage"),
                },
            )

            if not response.tool_calls:
                final = (response.content or "").strip()
                if not final:
                    raise AgentError("Model returned neither content nor tool calls")
                conversation.add_final(final)
                self._maybe_create_semantic_summary(conversation, step)
                self._emit("completed", {"step": step, "output": final})
                return AgentResult(
                    final_output=final,
                    steps=step,
                    tool_calls=tool_call_count,
                    messages=tuple(conversation.all_messages()),
                )

            assistant_message = self._assistant_message(response)
            tool_messages: list[dict[str, Any]] = []
            for call in response.tool_calls:
                self._check_cancelled()
                tool_call_count += 1
                fingerprint = (call.name, call.arguments)
                if fingerprint == previous_fingerprint:
                    repeated_calls += 1
                else:
                    repeated_calls = 1
                    previous_fingerprint = fingerprint

                if repeated_calls >= 3:
                    result_json = (
                        '{"success":false,"error":"The identical tool call was blocked '
                        'after three consecutive attempts.","metadata":{"code":'
                        '"RepeatedToolCall"}}'
                    )
                    self._emit(
                        "tool_finish",
                        {
                            "step": step,
                            "name": call.name,
                            "arguments": call.arguments,
                            "success": False,
                            "result": result_json,
                            "duration_ms": 0,
                            "error_code": "RepeatedToolCall",
                        },
                    )
                else:
                    self._emit(
                        "tool_start",
                        {"step": step, "name": call.name, "arguments": call.arguments},
                    )
                    tool_started = time.perf_counter()
                    result = self.tools.execute(call.name, call.arguments)
                    result_json = result.to_json()
                    reference = (
                        self.tools.archive_result(call.name, result_json)
                        if len(result_json) > HISTORICAL_TOOL_RESULT_LIMIT
                        else None
                    )
                    context_result_json, compression = compress_tool_result(
                        call.name,
                        result_json,
                        reference=reference,
                    )
                    self._emit(
                        "tool_finish",
                        {
                            "step": step,
                            "name": call.name,
                            "arguments": call.arguments,
                            "success": result.success,
                            "result": result_json,
                            "duration_ms": round(
                                (time.perf_counter() - tool_started) * 1_000, 2
                            ),
                            "error_code": result.metadata.get("code"),
                            "metadata": dict(result.metadata),
                            "context_compression": compression,
                        },
                    )

                if repeated_calls >= 3:
                    context_result_json, _ = compress_tool_result(call.name, result_json)

                tool_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": context_result_json,
                    }
                )
            conversation.add_tool_turn(assistant_message, tool_messages)
            self._maybe_create_semantic_summary(conversation, step)

        raise AgentLimitError(
            f"Agent reached the maximum of {self.max_steps} model steps without finishing"
        )

    @staticmethod
    def _assistant_message(response: ModelResponse) -> dict[str, Any]:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": response.content,
            "tool_calls": [call.as_message_item() for call in response.tool_calls],
        }
        if response.reasoning_content:
            message["reasoning_content"] = response.reasoning_content
        return message

    def _emit(self, event: str, payload: Mapping[str, Any]) -> None:
        if self.on_event is not None:
            self.on_event(event, payload)

    @staticmethod
    def _public_decision_summary(response: ModelResponse) -> str:
        """Return a concise, user-visible rationale without exposing hidden reasoning."""
        if not response.tool_calls:
            return "根据当前上下文，模型判断任务已完成，准备给出最终回答。"

        public_content = (response.content or "").strip()
        if public_content:
            if len(public_content) > 2_000:
                return public_content[:2_000] + "…"
            return public_content

        names = list(dict.fromkeys(call.name for call in response.tool_calls))
        tools = "、".join(names)
        return f"为继续推进任务，本轮决定调用 {tools} 获取信息或执行操作。"

    def _check_cancelled(self) -> None:
        if self.should_stop is not None and self.should_stop():
            raise AgentCancelledError("Agent run was cancelled")

    @staticmethod
    def _error_code(exc: Exception) -> str:
        text = str(exc)
        if "HTTP " in text:
            suffix = text.split("HTTP ", 1)[1].split(":", 1)[0].strip()
            if suffix.isdigit():
                return f"HTTP_{suffix}"
        return type(exc).__name__

    @staticmethod
    def _trace_messages(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Copy request messages without exposing provider hidden reasoning fields."""
        return [
            {key: value for key, value in message.items() if key != "reasoning_content"}
            for message in messages
        ]

    def _maybe_create_semantic_summary(
        self, conversation: Conversation, step: int
    ) -> None:
        if not conversation.semantic_summary_due():
            return
        messages = conversation.semantic_summary_request()
        started = time.perf_counter()
        self._emit(
            "semantic_summary_request",
            {
                "step": step,
                "message_count": len(messages),
                "request_messages": self._trace_messages(messages),
            },
        )
        try:
            response = self.model.complete(messages, [])
            if response.tool_calls or not (response.content or "").strip():
                raise ValueError("Summary model did not return a JSON text response")
            conversation.observe_usage(
                messages,
                response.metadata.get("usage"),
                include_tools=False,
            )
            conversation.apply_semantic_summary(response.content or "")
            self._emit(
                "semantic_summary_response",
                {
                    "step": step,
                    "duration_ms": round((time.perf_counter() - started) * 1_000, 2),
                    "model": response.metadata.get("model"),
                    "request_id": response.metadata.get("id"),
                    "usage": response.metadata.get("usage"),
                    "summary": conversation.to_state().get("semantic_summary"),
                },
            )
        except Exception as exc:
            conversation.mark_semantic_summary_attempted()
            self._emit(
                "semantic_summary_error",
                {
                    "step": step,
                    "duration_ms": round((time.perf_counter() - started) * 1_000, 2),
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
