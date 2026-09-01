"""Command-line interface."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any, Mapping

from .agent import Agent
from .config import Settings
from .conversation import Conversation
from .factory import build_registry
from .models import DeepSeekChatModel, ScriptedDemoModel
from .prompts import system_prompt_for_models


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


def run_cli(
    settings: Settings,
    workspace: Path,
    *,
    task: str = "",
    demo: bool = False,
    quiet: bool = False,
) -> int:
    """Run one agent task through the terminal interface."""
    registry = build_registry(
        workspace,
        settings.command_timeout,
        approval_handler=None if demo else terminal_approval,
        settings=settings,
    )
    model = ScriptedDemoModel() if demo else DeepSeekChatModel(settings)

    if not task:
        if not sys.stdin.isatty():
            task = sys.stdin.read().strip()
        else:
            task = input("Task: ").strip()
    if demo and not task:
        task = "Run the offline file-tool demonstration."

    agent = Agent(
        model,
        registry,
        max_steps=settings.max_steps,
        max_context_tokens=settings.context_tokens,
        on_event=None if quiet else event_printer,
    )
    primary_identity = "offline scripted demo" if demo else settings.model
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
