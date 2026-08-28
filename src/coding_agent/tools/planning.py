"""Structured task planning tool for complex agent work."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .base import ToolExecutionError, ToolResult, reject_unknown


PlanHandler = Callable[[Mapping[str, Any]], Mapping[str, Any] | None]
VALID_STATUSES = {"pending", "in_progress", "completed"}


class UpdatePlanTool:
    name = "update_plan"
    description = (
        "Create or update the execution plan for a complex task. Keep at most one step "
        "in_progress and mark each finished step completed as work proceeds."
    )
    parameters = {
        "type": "object",
        "properties": {
            "explanation": {
                "type": "string",
                "description": "Optional short reason for changing the plan.",
            },
            "plan": {
                "type": "array",
                "minItems": 1,
                "maxItems": 20,
                "items": {
                    "type": "object",
                    "properties": {
                        "step": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                        },
                    },
                    "required": ["step", "status"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["plan"],
        "additionalProperties": False,
    }

    def __init__(self, handler: PlanHandler) -> None:
        self.handler = handler

    def run(self, arguments: Mapping[str, Any]) -> ToolResult:
        reject_unknown(arguments, {"explanation", "plan"})
        explanation = arguments.get("explanation", "")
        if not isinstance(explanation, str) or len(explanation) > 2_000:
            raise ToolExecutionError(
                "InvalidArguments", "explanation must be a string up to 2,000 characters"
            )
        raw_plan = arguments.get("plan")
        if not isinstance(raw_plan, list) or not 1 <= len(raw_plan) <= 20:
            raise ToolExecutionError("InvalidArguments", "plan must contain 1 to 20 steps")
        plan: list[dict[str, str]] = []
        active = 0
        for index, raw_item in enumerate(raw_plan, start=1):
            if not isinstance(raw_item, Mapping) or set(raw_item) != {"step", "status"}:
                raise ToolExecutionError(
                    "InvalidArguments", f"plan item {index} must contain only step and status"
                )
            step = raw_item.get("step")
            status = raw_item.get("status")
            if not isinstance(step, str) or not step.strip() or len(step) > 500:
                raise ToolExecutionError(
                    "InvalidArguments", f"plan item {index} has an invalid step"
                )
            if status not in VALID_STATUSES:
                raise ToolExecutionError(
                    "InvalidArguments", f"plan item {index} has an invalid status"
                )
            active += status == "in_progress"
            plan.append({"step": step.strip(), "status": str(status)})
        if active > 1:
            raise ToolExecutionError("InvalidArguments", "only one step may be in_progress")
        payload = {"explanation": explanation.strip(), "plan": plan}
        stored = self.handler(payload)
        return ToolResult.ok(stored or payload)
