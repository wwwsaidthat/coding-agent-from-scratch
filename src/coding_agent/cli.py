"""Command-line interface."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

from .agent import Agent, AgentError
from .config import Settings, load_env_file
from .conversation import Conversation
from .models import DeepSeekChatModel, ModelAPIError, ScriptedDemoModel
from .prompts import system_prompt_for_models
from .tools import (
    AnalyzeImageTool,
    AnalyzePdfTool,
    FindFilesTool,
    ListFilesTool,
    MultiEditTool,
    QwenChatClient,
    ReadFileTool,
    ReplaceInFileTool,
    RunCommandTool,
    SearchCodeTool,
    ToolRegistry,
    UpdatePlanTool,
    WriteFileTool,
    WebSearchTool,
)
from .tools.filesystem import ApprovalHandler, WorkspacePaths
from .tools.planning import PlanHandler


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


def build_registry(
    workspace: Path,
    command_timeout: int,
    approval_handler: ApprovalHandler | None = None,
    settings: Settings | None = None,
    plan_handler: PlanHandler | None = None,
) -> ToolRegistry:
    paths = WorkspacePaths(workspace)
    tools = [
        FindFilesTool(paths),
        SearchCodeTool(paths),
        ListFilesTool(paths),
        ReadFileTool(paths),
        WriteFileTool(paths, approval_handler),
        ReplaceInFileTool(paths, approval_handler),
        MultiEditTool(paths, approval_handler),
        RunCommandTool(paths, default_timeout=command_timeout),
    ]
    if plan_handler is not None:
        tools.append(UpdatePlanTool(plan_handler))
    if settings is not None:
        external = QwenChatClient(settings)
        tools.extend(
            [
                WebSearchTool(external, approval_handler),
                AnalyzeImageTool(paths, external, approval_handler),
                AnalyzePdfTool(paths, external, approval_handler),
            ]
        )
    return ToolRegistry(tools)


def terminal_approval(proposal: Mapping[str, Any]) -> bool:
    """Ask a terminal user before edits or transfers to external services."""
    if not sys.stdin.isatty():
        return False
    print("\nApproval required:")
    print(str(proposal.get("title") or proposal.get("tool") or "Agent action"))
    if proposal.get("summary"):
        print(str(proposal["summary"]))
    details = proposal.get("details")
    if isinstance(details, Mapping):
        for name, value in details.items():
            print(f"  {name}: {value}")
    files = proposal.get("files")
    if isinstance(files, list):
        for file in files:
            if isinstance(file, Mapping):
                print(str(file.get("diff") or file.get("path") or ""))
    answer = input("Allow this action? [y/N] ").strip().lower()
    return answer in {"y", "yes"}


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
        registry = build_registry(
            workspace,
            settings.command_timeout,
            approval_handler=None if args.demo else terminal_approval,
            settings=settings,
        )
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
            max_context_tokens=settings.context_tokens,
            on_event=None if args.quiet else event_printer,
        )
        primary_identity = "offline scripted demo" if args.demo else settings.model
        conversation = Conversation(
            system_prompt_for_models(primary_identity, settings.qwen_model),
            max_context_tokens=settings.context_tokens,
        )
        result = agent.run(task, conversation=conversation)
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
