"""Application factories shared by the CLI and web entry points."""

from __future__ import annotations

from pathlib import Path

from .config import Settings
from .tools import (
    AnalyzeImageTool,
    AnalyzePdfTool,
    FindFilesTool,
    ListFilesTool,
    MultiEditTool,
    QwenChatClient,
    ReadFileTool,
    ReadToolResultTool,
    ReplaceInFileTool,
    RunCommandTool,
    SearchCodeTool,
    ToolRegistry,
    ToolResultArchive,
    UpdatePlanTool,
    WriteFileTool,
    WebSearchTool,
)
from .tools.filesystem import ApprovalHandler, WorkspacePaths
from .tools.planning import PlanHandler


def build_registry(
    workspace: Path,
    command_timeout: int,
    approval_handler: ApprovalHandler | None = None,
    settings: Settings | None = None,
    plan_handler: PlanHandler | None = None,
) -> ToolRegistry:
    """Build the concrete tool set for one workspace and interaction mode."""
    paths = WorkspacePaths(workspace)
    result_archive = ToolResultArchive(workspace)
    tools = [
        FindFilesTool(paths),
        SearchCodeTool(paths),
        ListFilesTool(paths),
        ReadFileTool(paths),
        WriteFileTool(paths, approval_handler),
        ReplaceInFileTool(paths, approval_handler),
        MultiEditTool(paths, approval_handler),
        RunCommandTool(paths, default_timeout=command_timeout),
        ReadToolResultTool(result_archive),
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
    return ToolRegistry(tools, result_archive=result_archive)
