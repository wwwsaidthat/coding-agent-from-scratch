"""Local archive and guarded range reader for complete tool results."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Any, Mapping
from uuid import uuid4

from .base import ToolExecutionError, ToolResult, optional_int, reject_unknown, required_string


RESULT_ID = re.compile(r"^[0-9a-f]{64}$")
MAX_RESULT_CHUNK_CHARS = 12_000


class ToolResultArchive:
    """Persist full results outside model context under content-addressed IDs."""

    def __init__(self, workspace: Path) -> None:
        self.root = workspace.resolve() / ".coding-agent" / "tool-results"
        self.root.mkdir(parents=True, exist_ok=True)

    def store(self, tool_name: str, result_json: str) -> dict[str, Any]:
        digest = hashlib.sha256(result_json.encode("utf-8")).hexdigest()
        target = self.root / f"{digest}.json"
        if not target.exists():
            temporary = target.with_name(f".{digest}.{uuid4().hex}.tmp")
            temporary.write_text(result_json, encoding="utf-8")
            temporary.chmod(0o600)
            try:
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        return {
            "result_id": digest,
            "tool_name": tool_name,
            "sha256": digest,
            "original_chars": len(result_json),
        }

    def read(self, result_id: str, start_char: int, max_chars: int) -> ToolResult:
        if not RESULT_ID.fullmatch(result_id):
            raise ToolExecutionError("InvalidResultId", "result_id must be a SHA-256 hex string")
        target = self.root / f"{result_id}.json"
        try:
            content = target.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ToolExecutionError("ResultNotFound", "Archived tool result was not found") from exc
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if digest != result_id:
            raise ToolExecutionError("ResultCorrupt", "Archived tool result failed SHA-256 verification")
        if start_char > len(content):
            raise ToolExecutionError(
                "InvalidArguments",
                f"start_char {start_char} exceeds result length {len(content)}",
            )
        end_char = min(len(content), start_char + max_chars)
        return ToolResult.ok(
            content[start_char:end_char],
            result_id=result_id,
            sha256=digest,
            total_chars=len(content),
            start_char=start_char,
            end_char=end_char,
            truncated=end_char < len(content),
            next_start_char=end_char if end_char < len(content) else None,
        )


class ReadToolResultTool:
    name = "read_tool_result"
    description = (
        "Read an exact character range from a complete local tool result that was compacted "
        "from context. Use the result_id in context_compression metadata."
    )
    parameters = {
        "type": "object",
        "properties": {
            "result_id": {"type": "string", "description": "SHA-256 result identifier."},
            "start_char": {"type": "integer", "minimum": 0},
            "max_chars": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_RESULT_CHUNK_CHARS,
                "description": "Maximum characters to return. Defaults to 8000.",
            },
        },
        "required": ["result_id"],
        "additionalProperties": False,
    }

    def __init__(self, archive: ToolResultArchive) -> None:
        self.archive = archive

    def run(self, arguments: Mapping[str, Any]) -> ToolResult:
        reject_unknown(arguments, {"result_id", "start_char", "max_chars"})
        result_id = required_string(arguments, "result_id")
        start_char = optional_int(
            arguments, "start_char", 0, minimum=0, maximum=100_000_000
        )
        max_chars = optional_int(
            arguments,
            "max_chars",
            8_000,
            minimum=1,
            maximum=MAX_RESULT_CHUNK_CHARS,
        )
        return self.archive.read(result_id, start_char, max_chars)
