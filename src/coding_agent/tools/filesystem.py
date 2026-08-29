"""Workspace-confined file tools."""

from __future__ import annotations

from dataclasses import dataclass
import difflib
import hashlib
import os
from pathlib import Path
import threading
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

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
MAX_DIFF_CHARS = 80_000
DENIED_NAMES = {
    ".env",
    ".coding-agent",
    ".git-credentials",
    "id_rsa",
    "id_ed25519",
}
IGNORED_DIRECTORIES = {
    ".git",
    ".coding-agent",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".agent-images",
    ".agent-files",
}


ApprovalHandler = Callable[[Mapping[str, Any]], bool]
EditApprovalHandler = ApprovalHandler

# 乐观锁的实现，读取文件之后并不会将文件锁住，但是如果用户在读取之后修改，这里会报conflict，后续模型会重新读文件
# 这里是实现将一个文件的快照转换成json的格式
@dataclass(frozen=True, slots=True)
class FileRevision:
    size: int
    modified_ns: int
    sha256: str

    def public(self) -> dict[str, Any]:
        return {
            "size": self.size,
            "modified_ns": self.modified_ns,
            "sha256": self.sha256,
        }


class WorkspacePaths:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve() # 转换成规范的绝对路径
        if not self.root.is_dir():
            raise ValueError(f"Workspace does not exist or is not a directory: {self.root}")
        self.edit_lock = threading.RLock()
        self._revisions: dict[Path, FileRevision] = {}

    def resolve(self, user_path: str, *, allow_missing: bool = False) -> Path:
        """防止工作范围超过工作区"""
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

    def remember_revision(self, path: Path, content: bytes) -> FileRevision:
        stat = path.stat()
        revision = FileRevision(
            size=len(content),
            modified_ns=stat.st_mtime_ns,
            sha256=hashlib.sha256(content).hexdigest(),
        )
        self._revisions[path] = revision
        return revision

    def require_current_revision(self, path: Path) -> tuple[bytes, FileRevision]:
        expected = self._revisions.get(path)
        if expected is None:
            raise ToolExecutionError(
                "ReadRequired",
                "File must be read with read_file before it can be edited",
            )
        try:
            content = path.read_bytes()
            stat = path.stat()
        except FileNotFoundError as exc:
            raise ToolExecutionError(
                "Conflict", "File changed or was removed after reading"
            ) from exc
        current = FileRevision(
            size=len(content),
            modified_ns=stat.st_mtime_ns,
            sha256=hashlib.sha256(content).hexdigest(),
        )
        if current != expected:
            raise ToolExecutionError(
                "Conflict",
                "File changed after it was read; read the latest version before editing",
            )
        return content, current


def _diff(relative: str, before: str, after: str) -> tuple[str, bool]:
    rendered = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{relative}",
            tofile=f"b/{relative}",
        )
    )
    truncated = len(rendered) > MAX_DIFF_CHARS
    if truncated:
        rendered = rendered[:MAX_DIFF_CHARS] + "\n… diff truncated …\n"
    return rendered or f"--- a/{relative}\n+++ b/{relative}\n(no textual change)\n", truncated


def _request_approval(
    handler: EditApprovalHandler | None,
    tool: str,
    files: Sequence[Mapping[str, Any]],
) -> None:
    if handler is None:
        return
    if not handler({"tool": tool, "files": list(files)}):
        raise ToolExecutionError("EditRejected", "User rejected the proposed code change")


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.coding-agent-{uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


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
            content = path.read_bytes()
            lines = content.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise ToolExecutionError("NotText", "File is not valid UTF-8 text") from exc
        with self.paths.edit_lock:
            revision = self.paths.remember_revision(path, content)

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
            revision=revision.public(),
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

    def __init__(
        self,
        paths: WorkspacePaths,
        approval_handler: EditApprovalHandler | None = None,
    ) -> None:
        self.paths = paths
        self.approval_handler = approval_handler

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
        with self.paths.edit_lock:
            existed = path.exists()
            if existed and not overwrite:
                raise ToolExecutionError(
                    "AlreadyExists", f"File already exists: {relative}; use overwrite=true"
                )
            if existed:
                old_bytes, _ = self.paths.require_current_revision(path)
                try:
                    old_content = old_bytes.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ToolExecutionError("NotText", "Existing file is not UTF-8") from exc
            else:
                old_content = ""
            rendered_diff, truncated = _diff(relative, old_content, content)

        _request_approval(
            self.approval_handler,
            self.name,
            [{"path": relative, "diff": rendered_diff, "truncated": truncated}],
        )

        with self.paths.edit_lock:
            if existed:
                self.paths.require_current_revision(path)
            elif path.exists():
                raise ToolExecutionError(
                    "Conflict", "File was created by another process before approval"
                )
            _atomic_write(path, content)
            revision = self.paths.remember_revision(path, encoded)
        return ToolResult.ok(
            f"Wrote {relative}",
            path=relative,
            bytes_written=len(encoded),
            revision=revision.public(),
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

    def __init__(
        self,
        paths: WorkspacePaths,
        approval_handler: EditApprovalHandler | None = None,
    ) -> None:
        self.paths = paths
        self.approval_handler = approval_handler

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
        with self.paths.edit_lock:
            if not path.is_file():
                raise ToolExecutionError("NotFile", f"Not a file: {relative}")
            old_bytes, _ = self.paths.require_current_revision(path)
            try:
                content = old_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ToolExecutionError("NotText", "File is not valid UTF-8 text") from exc
            actual = content.count(old_text)
            if actual != expected:
                raise ToolExecutionError(
                    "ReplacementCountMismatch",
                    f"Expected {expected} match(es), found {actual}; file was not changed",
                )
            updated = content.replace(old_text, new_text)
            updated_bytes = updated.encode("utf-8")
            if len(updated_bytes) > MAX_WRITE_BYTES:
                raise ToolExecutionError("FileTooLarge", "Updated file is larger than 1 MB")
            rendered_diff, truncated = _diff(relative, content, updated)

        _request_approval(
            self.approval_handler,
            self.name,
            [{"path": relative, "diff": rendered_diff, "truncated": truncated}],
        )

        with self.paths.edit_lock:
            self.paths.require_current_revision(path)
            _atomic_write(path, updated)
            revision = self.paths.remember_revision(path, updated_bytes)
        return ToolResult.ok(
            f"Updated {relative}",
            path=relative,
            replacements=actual,
            revision=revision.public(),
        )


class MultiEditTool:
    name = "multi_edit"
    description = (
        "Atomically apply multiple exact replacements after every target file has been read. "
        "All edits are validated before approval and committed together."
    )
    parameters = {
        "type": "object",
        "properties": {
            "edits": {
                "type": "array",
                "minItems": 1,
                "maxItems": 20,
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old_text": {"type": "string"},
                        "new_text": {"type": "string"},
                        "expected_replacements": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 100,
                        },
                    },
                    "required": ["path", "old_text", "new_text"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["edits"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        paths: WorkspacePaths,
        approval_handler: EditApprovalHandler | None = None,
    ) -> None:
        self.paths = paths
        self.approval_handler = approval_handler

    def run(self, arguments: Mapping[str, Any]) -> ToolResult:
        reject_unknown(arguments, {"edits"})
        raw_edits = arguments.get("edits")
        if not isinstance(raw_edits, list) or not 1 <= len(raw_edits) <= 20:
            raise ToolExecutionError("InvalidArguments", "'edits' must contain 1 to 20 items")

        parsed: list[tuple[str, str, str, int]] = []
        for raw in raw_edits:
            if not isinstance(raw, Mapping):
                raise ToolExecutionError("InvalidArguments", "Every edit must be an object")
            reject_unknown(
                raw, {"path", "old_text", "new_text", "expected_replacements"}
            )
            path = required_string(raw, "path")
            old_text = required_string(raw, "old_text")
            new_text = raw.get("new_text")
            if not isinstance(new_text, str):
                raise ToolExecutionError("InvalidArguments", "'new_text' must be a string")
            expected = optional_int(
                raw, "expected_replacements", 1, minimum=1, maximum=100
            )
            parsed.append((path, old_text, new_text, expected))

        with self.paths.edit_lock:
            originals: dict[Path, bytes] = {}
            updated_text: dict[Path, str] = {}
            relative_names: dict[Path, str] = {}
            replacements: dict[Path, int] = {}
            for relative, old_text, new_text, expected in parsed:
                path = self.paths.resolve(relative)
                if path not in originals:
                    original, _ = self.paths.require_current_revision(path)
                    originals[path] = original
                    relative_names[path] = relative
                    try:
                        updated_text[path] = original.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise ToolExecutionError("NotText", f"Not UTF-8: {relative}") from exc
                    replacements[path] = 0
                current = updated_text[path]
                actual = current.count(old_text)
                if actual != expected:
                    raise ToolExecutionError(
                        "ReplacementCountMismatch",
                        f"{relative}: expected {expected} match(es), found {actual}",
                    )
                updated_text[path] = current.replace(old_text, new_text)
                replacements[path] += actual

            proposals: list[dict[str, Any]] = []
            for path, updated in updated_text.items():
                encoded = updated.encode("utf-8")
                if len(encoded) > MAX_WRITE_BYTES:
                    raise ToolExecutionError(
                        "FileTooLarge",
                        f"Updated file is larger than 1 MB: {relative_names[path]}",
                    )
                before = originals[path].decode("utf-8")
                rendered, truncated = _diff(relative_names[path], before, updated)
                proposals.append(
                    {
                        "path": relative_names[path],
                        "diff": rendered,
                        "truncated": truncated,
                    }
                )

        _request_approval(self.approval_handler, self.name, proposals)

        with self.paths.edit_lock:
            for path in originals:
                self.paths.require_current_revision(path)
            staged: dict[Path, Path] = {}
            committed: list[Path] = []
            try:
                for path, updated in updated_text.items():
                    temporary = path.with_name(
                        f".{path.name}.multi-edit-{uuid4().hex}.tmp"
                    )
                    temporary.write_text(updated, encoding="utf-8")
                    staged[path] = temporary
                for path, temporary in staged.items():
                    temporary.replace(path)
                    committed.append(path)
            except OSError as exc:
                for path in committed:
                    rollback = path.with_name(
                        f".{path.name}.rollback-{uuid4().hex}.tmp"
                    )
                    rollback.write_bytes(originals[path])
                    rollback.replace(path)
                raise ToolExecutionError("AtomicWriteFailed", str(exc)) from exc
            finally:
                for temporary in staged.values():
                    temporary.unlink(missing_ok=True)

            revisions = {}
            for path, updated in updated_text.items():
                revision = self.paths.remember_revision(path, updated.encode("utf-8"))
                revisions[relative_names[path]] = revision.public()

        return ToolResult.ok(
            f"Updated {len(updated_text)} file(s) atomically",
            files=list(relative_names.values()),
            replacements=sum(replacements.values()),
            revisions=revisions,
        )
