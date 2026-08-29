"""Workspace-confined source discovery powered by ripgrep."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import time
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
from .filesystem import WorkspacePaths


SEARCH_TIMEOUT_SECONDS = 12
MAX_STDERR_CHARS = 4_000
EXCLUDED_GLOBS = (
    "!.git/**",
    "!.coding-agent/**",
    "!.venv/**",
    "!venv/**",
    "!node_modules/**",
    "!.agent-images/**",
    "!.agent-files/**",
    "!**/.env*",
)


def _rg_executable() -> str:
    executable = shutil.which("rg")
    if executable is None:
        raise ToolExecutionError(
            "SearchUnavailable",
            "ripgrep (rg) is required for code search but was not found on PATH",
        )
    return executable


def _safe_environment() -> dict[str, str]:
    allowed = {"HOME", "LANG", "LC_ALL", "PATH", "TMPDIR"}
    return {name: value for name, value in os.environ.items() if name in allowed}


def _target_argument(paths: WorkspacePaths, relative: str) -> str:
    resolved = paths.resolve(relative)
    if not resolved.exists():
        raise ToolExecutionError("NotFound", f"Path does not exist: {relative}")
    target = str(resolved.relative_to(paths.root))
    return target or "."


def _base_command() -> list[str]:
    command = [_rg_executable(), "--hidden"]
    for pattern in EXCLUDED_GLOBS:
        command.extend(["--glob", pattern])
    return command


class FindFilesTool:
    name = "find_files"
    description = (
        "Find workspace files by glob using ripgrep. Returns matching relative paths and "
        "whether the result was truncated. Prefer this over broad directory listings."
    )
    parameters = {
        "type": "object",
        "properties": {
            "glob": {
                "type": "string",
                "description": "Glob such as '**/*.py', 'src/**', or '*test*'.",
            },
            "path": {
                "type": "string",
                "description": "Workspace-relative search root. Defaults to '.'.",
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500,
                "description": "Maximum paths to return. Defaults to 200.",
            },
        },
        "required": ["glob"],
        "additionalProperties": False,
    }

    def __init__(self, paths: WorkspacePaths) -> None:
        self.paths = paths

    def run(self, arguments: Mapping[str, Any]) -> ToolResult:
        reject_unknown(arguments, {"glob", "path", "max_results"})
        pattern = required_string(arguments, "glob")
        relative = optional_string(arguments, "path", ".")
        maximum = optional_int(
            arguments, "max_results", 200, minimum=1, maximum=500
        )
        target = _target_argument(self.paths, relative)
        command = [*_base_command(), "--files", "--glob", pattern, target]

        process = self._start(command)
        files: list[str] = []
        started = time.monotonic()
        truncated = False
        try:
            assert process.stdout is not None
            for raw_line in process.stdout:
                if time.monotonic() - started > SEARCH_TIMEOUT_SECONDS:
                    raise ToolExecutionError("SearchTimeout", "File search exceeded 12 seconds")
                candidate = raw_line.rstrip("\r\n")
                try:
                    resolved = self.paths.resolve(candidate)
                except ToolExecutionError:
                    continue
                if not resolved.is_file():
                    continue
                if len(files) >= maximum:
                    truncated = True
                    process.terminate()
                    break
                files.append(candidate.removeprefix("./"))
            return_code = process.wait(timeout=2)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            raise ToolExecutionError("SearchTimeout", "File search did not stop cleanly") from exc
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

        stderr = self._stderr(process)
        if return_code not in {0, -15}:
            raise ToolExecutionError("SearchFailed", stderr or f"rg exited with {return_code}")
        return ToolResult.ok(
            {"files": files, "count": len(files), "truncated": truncated},
            engine="rg",
            query_glob=pattern,
            path=relative,
        )

    def _start(self, command: list[str]) -> subprocess.Popen[str]:
        try:
            return subprocess.Popen(
                command,
                cwd=self.paths.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                env=_safe_environment(),
            )
        except OSError as exc:
            raise ToolExecutionError("SearchFailed", str(exc)) from exc

    @staticmethod
    def _stderr(process: subprocess.Popen[str]) -> str:
        try:
            if process.stderr is None:
                return ""
            return process.stderr.read(MAX_STDERR_CHARS).strip()
        finally:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()


class SearchCodeTool:
    name = "search_code"
    description = (
        "Search source text with ripgrep and return structured file, line, column, text, "
        "and truncation information. Literal matching is the safe default."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Text or regex to search for."},
            "path": {
                "type": "string",
                "description": "Workspace-relative search root. Defaults to '.'.",
            },
            "glob": {
                "type": "string",
                "description": "Optional file glob, for example '*.py' or 'src/**'.",
            },
            "regex": {
                "type": "boolean",
                "description": "Interpret query as regex. Defaults to false.",
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500,
                "description": "Maximum matches to return. Defaults to 200.",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, paths: WorkspacePaths) -> None:
        self.paths = paths

    def run(self, arguments: Mapping[str, Any]) -> ToolResult:
        reject_unknown(arguments, {"query", "path", "glob", "regex", "max_results"})
        query = required_string(arguments, "query")
        relative = optional_string(arguments, "path", ".")
        pattern = optional_string(arguments, "glob", "")
        regex = optional_bool(arguments, "regex", False)
        maximum = optional_int(
            arguments, "max_results", 200, minimum=1, maximum=500
        )
        target = _target_argument(self.paths, relative)
        command = [*_base_command(), "--json", "--line-number", "--column"]
        if not regex:
            command.append("--fixed-strings")
        if pattern:
            command.extend(["--glob", pattern])
        command.extend(["--", query, target])

        process = FindFilesTool(self.paths)._start(command)
        matches: list[dict[str, Any]] = []
        started = time.monotonic()
        truncated = False
        try:
            assert process.stdout is not None
            for raw_line in process.stdout:
                if time.monotonic() - started > SEARCH_TIMEOUT_SECONDS:
                    raise ToolExecutionError("SearchTimeout", "Code search exceeded 12 seconds")
                try:
                    message = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if message.get("type") != "match":
                    continue
                data = message.get("data", {})
                path_data = data.get("path", {})
                lines_data = data.get("lines", {})
                candidate = path_data.get("text")
                if not isinstance(candidate, str):
                    continue
                try:
                    resolved = self.paths.resolve(candidate)
                except ToolExecutionError:
                    continue
                if not resolved.is_file():
                    continue
                submatches = data.get("submatches") or []
                start = submatches[0].get("start", 0) if submatches else 0
                if len(matches) >= maximum:
                    truncated = True
                    process.terminate()
                    break
                matches.append(
                    {
                        "path": candidate.removeprefix("./"),
                        "line": data.get("line_number"),
                        "column": int(start) + 1,
                        "text": str(lines_data.get("text", "")).rstrip("\r\n"),
                    }
                )
            return_code = process.wait(timeout=2)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            raise ToolExecutionError("SearchTimeout", "Code search did not stop cleanly") from exc
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

        stderr = FindFilesTool._stderr(process)
        if return_code not in {0, 1, -15}:
            raise ToolExecutionError("SearchFailed", stderr or f"rg exited with {return_code}")
        return ToolResult.ok(
            {"matches": matches, "count": len(matches), "truncated": truncated},
            engine="rg",
            query=query,
            regex=regex,
            glob=pattern or None,
            path=relative,
        )
