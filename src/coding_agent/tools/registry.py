"""Tool registration, schema export, and guarded dispatch."""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from .base import Tool, ToolExecutionError, ToolResult
from .result_archive import ToolResultArchive


class ToolRegistry:
    def __init__(
        self,
        tools: Iterable[Tool],
        *,
        result_archive: ToolResultArchive | None = None,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        self._result_archive = result_archive
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"Duplicate tool name: {tool.name}")
            self._tools[tool.name] = tool

    def archive_result(self, tool_name: str, result_json: str) -> dict[str, Any] | None:
        if self._result_archive is None:
            return None
        return self._result_archive.store(tool_name, result_json)

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
