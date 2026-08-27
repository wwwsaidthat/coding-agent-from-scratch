"""Common types and validation helpers for local tools."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Mapping, Protocol


JsonObject = dict[str, Any]


class ToolExecutionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ToolResult:
    success: bool
    data: Any = None
    error: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, data: Any, **metadata: Any) -> "ToolResult":
        return cls(success=True, data=data, metadata=metadata)

    @classmethod
    def fail(cls, code: str, message: str) -> "ToolResult":
        return cls(success=False, error=message, metadata={"code": code})

    def to_json(self) -> str:
        payload: JsonObject = {"success": self.success}
        if self.success:
            payload["data"] = self.data
        else:
            payload["error"] = self.error
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return json.dumps(payload, ensure_ascii=False)


class Tool(Protocol):
    name: str
    description: str
    parameters: Mapping[str, Any]

    def run(self, arguments: Mapping[str, Any]) -> ToolResult:
        """Execute a validated local action."""


def reject_unknown(arguments: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = set(arguments) - allowed
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ToolExecutionError("InvalidArguments", f"Unknown arguments: {names}")


def required_string(arguments: Mapping[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value:
        raise ToolExecutionError("InvalidArguments", f"'{name}' must be a non-empty string")
    return value


def optional_string(arguments: Mapping[str, Any], name: str, default: str) -> str:
    value = arguments.get(name, default)
    if not isinstance(value, str):
        raise ToolExecutionError("InvalidArguments", f"'{name}' must be a string")
    return value


def optional_int(
    arguments: Mapping[str, Any],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolExecutionError("InvalidArguments", f"'{name}' must be an integer")
    if not minimum <= value <= maximum:
        raise ToolExecutionError(
            "InvalidArguments", f"'{name}' must be between {minimum} and {maximum}"
        )
    return value


def optional_bool(arguments: Mapping[str, Any], name: str, default: bool) -> bool:
    value = arguments.get(name, default)
    if not isinstance(value, bool):
        raise ToolExecutionError("InvalidArguments", f"'{name}' must be a boolean")
    return value
