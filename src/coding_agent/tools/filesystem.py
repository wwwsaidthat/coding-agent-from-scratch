"""Workspace-confined file tools."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from .base import (
    ToolExecutionError,
    ToolResult,
    optional_bool,
    optional_int,
    optional_string,
    reject_unknown,
    required_string,
)


MAX_READ_BYTES = 1_000_000
MAX_WRITE_BYTES = 1_000_000
MAX_LIST_ENTRIES = 500
DENIED_NAMES = {".env", ".git-credentials", "id_rsa", "id_ed25519"}
IGNORED_DIRECTORIES = {".git", ".venv", "venv", "__pycache__", "node_modules"}


class WorkspacePaths:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        if not self.root.is_dir():
            raise ValueError(f"Workspace does not exist or is not a directory: {self.root}")

    def resolve(self, user_path: str, *, allow_missing: bool = False) -> Path:
        del allow_missing  # Kept in the public helper signature for caller clarity.
        path = Path(user_path)
        if path.is_absolute():
            raise ToolExecutionError("PathDenied", "Absolute paths are not allowed")
        # strict=False still resolves existing symlinks, so symlink escapes are rejected,
        # while missing paths can be reported by each tool with a useful error code.
        candidate = (self.root / path).resolve(strict=False)
        if candidate != self.root and self.root not in candidate.parents:
            raise ToolExecutionError("PathDenied", "Path escapes the workspace")
        self._reject_sensitive(candidate)
        return candidate

    def _reject_sensitive(self, path: Path) -> None:
        relative_parts = path.relative_to(self.root).parts if path != self.root else ()
        for part in relative_parts:
            if part in DENIED_NAMES or part.startswith(".env"):
                raise ToolExecutionError("SensitivePath", f"Access denied: {part}")


class ListFilesTool:
    name = "list_files"
    description = (
        "List files and directories inside the workspace. Use this before reading files "
        "when the project structure is unknown."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative directory path. Defaults to '.'.",
            },
            "max_depth": {
                "type": "integer",
                "minimum": 1,
                "maximum": 8,
                "description": "Maximum directory depth. Defaults to 4.",
            },
        },
        "additionalProperties": False,
    }

    def __init__(self, paths: WorkspacePaths) -> None:
        self.paths = paths

    def run(self, arguments: Mapping[str, Any]) -> ToolResult:
        reject_unknown(arguments, {"path", "max_depth"})
        relative = optional_string(arguments, "path", ".")
        max_depth = optional_int(arguments, "max_depth", 4, minimum=1, maximum=8)
        directory = self.paths.resolve(relative)
        if not directory.is_dir():
            raise ToolExecutionError("NotDirectory", f"Not a directory: {relative}")

        entries: list[str] = []
        base_depth = len(directory.relative_to(self.paths.root).parts)
        for current, dirnames, filenames in os.walk(directory):
            current_path = Path(current)
            depth = len(current_path.relative_to(self.paths.root).parts) - base_depth
            dirnames[:] = sorted(
                name for name in dirnames if name not in IGNORED_DIRECTORIES
            )
            if depth >= max_depth:
                dirnames[:] = []

            relative_current = current_path.relative_to(self.paths.root)
            for name in dirnames:
                entries.append(str(relative_current / name) + "/")
            for name in sorted(filenames):
                if name in DENIED_NAMES or name.startswith(".env"):
                    continue
                entries.append(str(relative_current / name))
            if len(entries) >= MAX_LIST_ENTRIES:
                entries = entries[:MAX_LIST_ENTRIES]
                break

        cleaned = [entry.removeprefix("./") for entry in entries]
        return ToolResult.ok(
            "\n".join(cleaned) if cleaned else "(empty directory)",
            entry_count=len(cleaned),
            truncated=len(entries) >= MAX_LIST_ENTRIES,
        )


class ReadFileTool:
    name = "read_file"
    description = (
        "Read a UTF-8 text file inside the workspace. Returns numbered lines and supports "
        "an inclusive start_line/end_line range."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative file path."},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, paths: WorkspacePaths) -> None:
        self.paths = paths

    def run(self, arguments: Mapping[str, Any]) -> ToolResult:
        reject_unknown(arguments, {"path", "start_line", "end_line"})
        relative = required_string(arguments, "path")
        start = optional_int(arguments, "start_line", 1, minimum=1, maximum=1_000_000)
        end = optional_int(
            arguments, "end_line", start + 399, minimum=1, maximum=1_000_000
        )
        if end < start:
            raise ToolExecutionError("InvalidArguments", "end_line must be >= start_line")
        path = self.paths.resolve(relative)
        if not path.is_file():
            raise ToolExecutionError("NotFile", f"Not a file: {relative}")
        if path.stat().st_size > MAX_READ_BYTES:
            raise ToolExecutionError("FileTooLarge", "File is larger than 1 MB")

        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise ToolExecutionError("NotText", "File is not valid UTF-8 text") from exc

        selected = lines[start - 1 : end]
        numbered = "\n".join(
            f"{number:>6} | {line}" for number, line in enumerate(selected, start=start)
        )
        return ToolResult.ok(
            numbered,
            path=relative,
            total_lines=len(lines),
            start_line=start,
            end_line=min(end, len(lines)),
            truncated=end < len(lines),
        )


class WriteFileTool:
    name = "write_file"
    description = (
        "Create a UTF-8 text file inside the workspace. Existing files require "
        "overwrite=true. Prefer replace_in_file for small edits to existing files."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative file path."},
            "content": {"type": "string", "description": "Complete UTF-8 file content."},
            "overwrite": {
                "type": "boolean",
                "description": "Whether an existing file may be replaced. Defaults to false.",
            },
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    def __init__(self, paths: WorkspacePaths) -> None:
        self.paths = paths

    def run(self, arguments: Mapping[str, Any]) -> ToolResult:
        reject_unknown(arguments, {"path", "content", "overwrite"})
        relative = required_string(arguments, "path")
        content = arguments.get("content")
        if not isinstance(content, str):
            raise ToolExecutionError("InvalidArguments", "'content' must be a string")
        overwrite = optional_bool(arguments, "overwrite", False)
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_WRITE_BYTES:
            raise ToolExecutionError("FileTooLarge", "Content is larger than 1 MB")

        path = self.paths.resolve(relative, allow_missing=True)
        if path.exists() and not overwrite:
            raise ToolExecutionError(
                "AlreadyExists", f"File already exists: {relative}; use overwrite=true"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".coding-agent.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
        return ToolResult.ok(
            f"Wrote {relative}", path=relative, bytes_written=len(encoded)
        )


class ReplaceInFileTool:
    name = "replace_in_file"
    description = (
        "Replace exact text in an existing UTF-8 file. The operation fails unless the "
        "number of matches equals expected_replacements."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative file path."},
            "old_text": {"type": "string", "description": "Exact text to find."},
            "new_text": {"type": "string", "description": "Replacement text."},
            "expected_replacements": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "Required match count. Defaults to 1.",
            },
        },
        "required": ["path", "old_text", "new_text"],
        "additionalProperties": False,
    }

    def __init__(self, paths: WorkspacePaths) -> None:
        self.paths = paths

    def run(self, arguments: Mapping[str, Any]) -> ToolResult:
        reject_unknown(
            arguments, {"path", "old_text", "new_text", "expected_replacements"}
        )
        relative = required_string(arguments, "path")
        old_text = required_string(arguments, "old_text")
        new_text = arguments.get("new_text")
        if not isinstance(new_text, str):
            raise ToolExecutionError("InvalidArguments", "'new_text' must be a string")
        expected = optional_int(
            arguments, "expected_replacements", 1, minimum=1, maximum=100
        )
        path = self.paths.resolve(relative)
        if not path.is_file():
            raise ToolExecutionError("NotFile", f"Not a file: {relative}")
        if path.stat().st_size > MAX_READ_BYTES:
            raise ToolExecutionError("FileTooLarge", "File is larger than 1 MB")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ToolExecutionError("NotText", "File is not valid UTF-8 text") from exc

        actual = content.count(old_text)
        if actual != expected:
            raise ToolExecutionError(
                "ReplacementCountMismatch",
                f"Expected {expected} match(es), found {actual}; file was not changed",
            )
        updated = content.replace(old_text, new_text)
        if len(updated.encode("utf-8")) > MAX_WRITE_BYTES:
            raise ToolExecutionError("FileTooLarge", "Updated file would be larger than 1 MB")
        temporary = path.with_name(path.name + ".coding-agent.tmp")
        temporary.write_text(updated, encoding="utf-8")
        temporary.replace(path)
        return ToolResult.ok(
            f"Updated {relative}", path=relative, replacements=actual
        )
