"""Model protocol and DeepSeek Chat Completions implementation."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import time
from typing import Any, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import Settings


JsonObject = dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str

    def as_message_item(self) -> JsonObject:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass(frozen=True, slots=True)
class ModelResponse:
    content: str | None
    tool_calls: tuple[ToolCall, ...] = ()
    reasoning_content: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


class ChatModel(Protocol):
    def complete(
        self, messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]]
    ) -> ModelResponse:
        """Return one assistant turn."""


class ModelAPIError(RuntimeError):
    """Raised when a model request fails or returns malformed data."""


class DeepSeekChatModel:
    """Minimal DeepSeek-compatible HTTP client with no agent SDK dependency."""

    def __init__(self, settings: Settings, *, retries: int = 2) -> None:
        self.settings = settings.require_api_key()
        self.retries = retries

    @property
    def endpoint(self) -> str:
        base = self.settings.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def complete(
        self, messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]]
    ) -> ModelResponse:
        payload: JsonObject = {
            "model": self.settings.model,
            "messages": list(messages),
            "thinking": {
                "type": self.settings.thinking,
                "reasoning_effort": self.settings.reasoning_effort,
            },
        }
        # Milestone summaries are ordinary, tool-free model calls. Some compatible
        # APIs reject an empty tools array, so only declare tool calling when needed.
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = "auto"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.settings.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "coding-agent-from-scratch/0.4.0",
            },
        )

        for attempt in range(self.retries + 1):
            try:
                with urlopen(request, timeout=self.settings.api_timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
                return self._parse_response(data)
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:2_000]
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if retryable and attempt < self.retries:
                    time.sleep(2**attempt)
                    continue
                raise ModelAPIError(f"DeepSeek HTTP {exc.code}: {detail}") from exc
            except URLError as exc:
                if attempt < self.retries:
                    time.sleep(2**attempt)
                    continue
                raise ModelAPIError(f"DeepSeek connection failed: {exc.reason}") from exc
            except TimeoutError as exc:
                if attempt < self.retries:
                    time.sleep(2**attempt)
                    continue
                raise ModelAPIError("DeepSeek request timed out") from exc
            except json.JSONDecodeError as exc:
                raise ModelAPIError("DeepSeek returned invalid JSON") from exc

        raise ModelAPIError("DeepSeek request failed after retries")

    @staticmethod
    def _parse_response(data: Mapping[str, Any]) -> ModelResponse:
        try:
            message = data["choices"][0]["message"]
            raw_calls = message.get("tool_calls") or []
            calls = tuple(
                ToolCall(
                    id=item["id"],
                    name=item["function"]["name"],
                    arguments=item["function"]["arguments"],
                )
                for item in raw_calls
            )
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelAPIError("DeepSeek response has an unexpected shape") from exc

        return ModelResponse(
            content=message.get("content"),
            reasoning_content=message.get("reasoning_content"),
            tool_calls=calls,
            metadata={
                "id": data.get("id"),
                "model": data.get("model"),
                "usage": data.get("usage"),
            },
        )


class ScriptedDemoModel:
    """Offline model used to verify the full tool loop without an API key."""

    def __init__(self) -> None:
        self.turn = 0

    def complete(
        self, messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]]
    ) -> ModelResponse:
        del messages, tools
        self.turn += 1
        if self.turn == 1:
            return ModelResponse(
                content=None,
                tool_calls=(ToolCall("demo-1", "list_files", '{"path":"."}'),),
            )
        if self.turn == 2:
            return ModelResponse(
                content=None,
                tool_calls=(
                    ToolCall("demo-2", "read_file", '{"path":"agent_demo.txt"}'),
                ),
            )
        if self.turn == 3:
            return ModelResponse(
                content=None,
                tool_calls=(
                    ToolCall(
                        "demo-3",
                        "write_file",
                        json.dumps(
                            {
                                "path": "agent_demo.txt",
                                "content": "Created by the offline agent demo.\n",
                                "overwrite": True,
                            }
                        ),
                    ),
                ),
            )
        if self.turn == 4:
            return ModelResponse(
                content=None,
                tool_calls=(
                    ToolCall("demo-4", "read_file", '{"path":"agent_demo.txt"}'),
                ),
            )
        return ModelResponse(
            content="Offline demo completed: listed files, wrote agent_demo.txt, and read it back."
        )
