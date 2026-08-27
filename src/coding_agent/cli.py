"""Command-line interface."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

from .agent import Agent, AgentError
from .config import Settings, load_env_file
from .models import DeepSeekChatModel, ModelAPIError, ScriptedDemoModel
from .tools import (
    ListFilesTool,
    ReadFileTool,
    ReplaceInFileTool,
    RunCommandTool,
    ToolRegistry,
    WriteFileTool,
)
from .tools.filesystem import WorkspacePaths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coding-agent",
        description="A lightweight coding agent built without an agent framework.",
    )
    parser.add_argument("task", nargs="*", help="Programming task for the agent")
    parser.add_argument(
        "--workspace",
        default=".",
        help="Directory the agent may access (default: current directory)",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=None,
        help="Maximum model turns before stopping",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run a deterministic offline tool-loop demo without an API key",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Hide intermediate model and tool events",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Start the local visual web interface",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Web server host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Web server port (default: 8765)",
    )
    return parser


def build_registry(workspace: Path, command_timeout: int) -> ToolRegistry:
    paths = WorkspacePaths(workspace)
    return ToolRegistry(
        [
            ListFilesTool(paths),
            ReadFileTool(paths),
            WriteFileTool(paths),
            ReplaceInFileTool(paths),
            RunCommandTool(paths, default_timeout=command_timeout),
        ]
    )


def event_printer(event: str, payload: Mapping[str, Any]) -> None:
    if event == "model_request":
        print(f"\n[step {payload['step']}] asking model...", flush=True)
    elif event == "tool_start":
        arguments = str(payload["arguments"])
        if len(arguments) > 300:
            arguments = arguments[:300] + "..."
        print(f"  -> {payload['name']} {arguments}", flush=True)
    elif event == "tool_finish":
        status = "ok" if payload["success"] else "error"
        result = str(payload["result"])
        if len(result) > 500:
            result = result[:500] + "..."
        print(f"  <- {status}: {result}", flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        load_env_file(Path.cwd() / ".env")
        settings = Settings.from_env().with_max_steps(args.max_steps)
        workspace = Path(args.workspace).expanduser().resolve()
        if args.web:
            from .webapp import run_web_server

            return run_web_server(
                settings,
                host=args.host,
                port=args.port,
                default_workspace=workspace,
            )
        registry = build_registry(workspace, settings.command_timeout)
        model = ScriptedDemoModel() if args.demo else DeepSeekChatModel(settings)

        task = " ".join(args.task).strip()
        if not task:
            if not sys.stdin.isatty():
                task = sys.stdin.read().strip()
            else:
                task = input("Task: ").strip()
        if args.demo and not task:
            task = "Run the offline file-tool demonstration."

        agent = Agent(
            model,
            registry,
            max_steps=settings.max_steps,
            on_event=None if args.quiet else event_printer,
        )
        result = agent.run(task)
        print("\nResult:\n" + result.final_output)
        print(
            f"\nCompleted in {result.steps} model step(s) and "
            f"{result.tool_calls} tool call(s)."
        )
        return 0
    except (ValueError, AgentError, ModelAPIError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
