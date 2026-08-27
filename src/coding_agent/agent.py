"""The framework-free model/tool execution loop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .conversation import Conversation
from .models import ChatModel, ModelResponse, ToolCall
from .prompts import SYSTEM_PROMPT
from .tools.registry import ToolRegistry


EventHandler = Callable[[str, Mapping[str, Any]], None]


class AgentError(RuntimeError):
    """Base class for controlled agent failures."""


class AgentLimitError(AgentError):
    """Raised when the local step budget is exhausted."""


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
        on_event: EventHandler | None = None,
    ) -> None:
        if max_steps <= 0:
            raise ValueError("max_steps must be greater than zero")
        self.model = model
        self.tools = tools
        self.max_steps = max_steps
        self.max_context_chars = max_context_chars
        self.on_event = on_event

    def run(self, task: str) -> AgentResult:
        if not task.strip():
            raise ValueError("Task must not be empty")

        conversation = Conversation(
            SYSTEM_PROMPT,
            task.strip(),
            max_context_chars=self.max_context_chars,
        )
        tool_call_count = 0
        previous_fingerprint: tuple[str, str] | None = None
        repeated_calls = 0

        for step in range(1, self.max_steps + 1):
            self._emit("model_request", {"step": step})
            response = self.model.complete(
                conversation.api_messages(), self.tools.definitions
            )
            self._emit(
                "model_response",
                {"step": step, "tool_call_count": len(response.tool_calls)},
            )

            if not response.tool_calls:
                final = (response.content or "").strip()
                if not final:
                    raise AgentError("Model returned neither content nor tool calls")
                conversation.add_final(final)
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
                else:
                    self._emit(
                        "tool_start",
                        {"step": step, "name": call.name, "arguments": call.arguments},
                    )
                    result = self.tools.execute(call.name, call.arguments)
                    result_json = result.to_json()
                    self._emit(
                        "tool_finish",
                        {
                            "step": step,
                            "name": call.name,
                            "success": result.success,
                            "result": result_json,
                        },
                    )

                tool_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": result_json,
                    }
                )
            conversation.add_tool_turn(assistant_message, tool_messages)

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
