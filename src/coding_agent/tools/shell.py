"""Constrained subprocess execution without invoking a shell."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from typing import Any, Mapping

from .base import ToolExecutionError, ToolResult, optional_int, optional_string, reject_unknown
from .filesystem import WorkspacePaths


DEFAULT_ALLOWED_COMMANDS = {
    "cargo",
    "git",
    "go",
    "node",
    "npm",
    "npx",
    "pnpm",
    "python",
    "python3",
    "pytest",
    "ruby",
    "ruff",
    "uv",
}
BLOCKED_GIT_SUBCOMMANDS = {
    "clean",
    "push",
    "rebase",
    "reset",
    "restore",
}
MAX_OUTPUT_CHARS = 30_000


class RunCommandTool:
    name = "run_command"
    description = (
        "Run one allow-listed command in the workspace without a shell. Pass argv as a "
        "JSON array, for example [\"python3\", \"-m\", \"unittest\"]. Pipes, redirects, "
        "remote git operations, and destructive git commands are not supported."
    )
    parameters = {
        "type": "object",
        "properties": {
            "argv": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 50,
                "description": "Executable and arguments as separate strings.",
            },
            "cwd": {
                "type": "string",
                "description": "Workspace-relative working directory. Defaults to '.'.",
            },
            "timeout_seconds": {
                "type": "integer",
                "minimum": 1,
                "maximum": 120,
            },
        },
        "required": ["argv"],
        "additionalProperties": False,
    }

    def __init__(self, paths: WorkspacePaths, default_timeout: int = 30) -> None:
        self.paths = paths
        self.default_timeout = min(max(default_timeout, 1), 120)
        extra = os.getenv("CODING_AGENT_ALLOWED_COMMANDS", "")
        self.allowed_commands = DEFAULT_ALLOWED_COMMANDS | {
            item.strip() for item in extra.split(",") if item.strip()
        }

    def run(self, arguments: Mapping[str, Any]) -> ToolResult:
        reject_unknown(arguments, {"argv", "cwd", "timeout_seconds"})
        argv = arguments.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or len(argv) > 50
            or any(not isinstance(item, str) or not item for item in argv)
        ):
            raise ToolExecutionError(
                "InvalidArguments", "'argv' must be a non-empty array of strings"
            )

        executable = Path(argv[0]).name
        if executable not in self.allowed_commands:
            raise ToolExecutionError(
                "CommandDenied",
                f"Command '{executable}' is not allow-listed. Set "
                "CODING_AGENT_ALLOWED_COMMANDS to add trusted commands.",
            )
        self._enforce_command_policy(executable, argv)

        relative_cwd = optional_string(arguments, "cwd", ".")
        cwd = self.paths.resolve(relative_cwd)
        if not cwd.is_dir():
            raise ToolExecutionError("NotDirectory", f"Not a directory: {relative_cwd}")
        timeout = optional_int(
            arguments,
            "timeout_seconds",
            self.default_timeout,
            minimum=1,
            maximum=120,
        )

        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
                env=self._safe_environment(),
            )
        except FileNotFoundError as exc:
            raise ToolExecutionError("CommandNotFound", f"Command not found: {argv[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            partial = ((exc.stdout or "") + (exc.stderr or ""))[:MAX_OUTPUT_CHARS]
            return ToolResult.fail(
                "CommandTimeout", f"Command exceeded {timeout}s timeout. Output:\n{partial}"
            )

        stdout = completed.stdout
        stderr = completed.stderr
        combined = (stdout + ("\n" if stdout and stderr else "") + stderr).strip()
        truncated = len(combined) > MAX_OUTPUT_CHARS
        if truncated:
            combined = combined[:MAX_OUTPUT_CHARS] + "\n... output truncated ..."
        metadata = {
            "exit_code": completed.returncode,
            "command": argv,
            "cwd": str(cwd.relative_to(self.paths.root) or "."),
            "output_truncated": truncated,
        }
        if completed.returncode != 0:
            return ToolResult(
                success=False,
                error=combined or f"Command exited with code {completed.returncode}",
                metadata=metadata,
            )
        return ToolResult.ok(combined or "(no output)", **metadata)

    @staticmethod
    def _enforce_command_policy(executable: str, argv: list[str]) -> None:
        if executable == "git" and len(argv) > 1 and argv[1] in BLOCKED_GIT_SUBCOMMANDS:
            raise ToolExecutionError(
                "CommandDenied", f"git {argv[1]} is blocked by the command policy"
            )
        if executable in {"python", "python3"} and "-c" in argv[1:]:
            raise ToolExecutionError("CommandDenied", "python -c is blocked")
        if executable == "node" and "-e" in argv[1:]:
            raise ToolExecutionError("CommandDenied", "node -e is blocked")

    @staticmethod
    def _safe_environment() -> dict[str, str]:
        allowed_names = {
            "COMSPEC",
            "HOME",
            "LANG",
            "LC_ALL",
            "PATH",
            "PATHEXT",
            "PYTHONPATH",
            "SYSTEMROOT",
            "TMPDIR",
            "VIRTUAL_ENV",
            "WINDIR",
        }
        return {name: value for name, value in os.environ.items() if name in allowed_names}
