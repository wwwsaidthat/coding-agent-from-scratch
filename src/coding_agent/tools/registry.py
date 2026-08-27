"""Tool registration, schema export, and guarded dispatch."""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from .base import Tool, ToolExecutionError, ToolResult


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool]) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"Duplicate tool name: {tool.name}")
            self._tools[tool.name] = tool

    @property
    def definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": dict(tool.parameters),
                },
            }
            for tool in self._tools.values()
        ]

    def execute(self, name: str, raw_arguments: str) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult.fail("UnknownTool", f"Unknown tool: {name}")

        try:
            parsed = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError as exc:
            return ToolResult.fail(
                "InvalidJSON", f"Tool arguments are not valid JSON: {exc.msg}"
            )
        if not isinstance(parsed, Mapping):
            return ToolResult.fail("InvalidArguments", "Tool arguments must be a JSON object")

        try:
            return tool.run(parsed)
        except ToolExecutionError as exc:
            return ToolResult.fail(exc.code, exc.message)
        except Exception as exc:  # Keep the agent loop alive on unexpected tool failures.
            return ToolResult.fail("ToolFailure", f"{type(exc).__name__}: {exc}")
