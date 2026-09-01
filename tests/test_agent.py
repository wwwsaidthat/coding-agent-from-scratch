from pathlib import Path
import json
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from coding_agent.agent import Agent, AgentCancelledError, AgentError, AgentLimitError
from coding_agent.cli import main
from coding_agent.conversation import Conversation
from coding_agent.factory import build_registry
from coding_agent.models import ModelResponse, ScriptedDemoModel, ToolCall
from coding_agent.tools.base import ToolResult
from coding_agent.tools.registry import ToolRegistry
from coding_agent.tools.result_archive import ReadToolResultTool, ToolResultArchive


class AlwaysCallsToolModel:
    def complete(self, messages, tools):
        del messages, tools
        return ModelResponse(
            content=None,
            tool_calls=(ToolCall("repeat", "list_files", '{"path":"."}'),),
        )


class EmptyModel:
    def complete(self, messages, tools):
        del messages, tools
        return ModelResponse(content=None)


class ContextRecordingModel:
    def __init__(self):
        self.requests = []

    def complete(self, messages, tools):
        del tools
        self.requests.append(messages)
        return ModelResponse(content="turn complete")


class UsageReportingModel:
    def complete(self, messages, tools):
        del messages, tools
        return ModelResponse(
            content="usage recorded",
            metadata={"usage": {"prompt_tokens": 321, "completion_tokens": 7}},
        )


class LargeOutputTool:
    name = "large_output"
    description = "Return a deliberately large test value."
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}

    def run(self, arguments):
        del arguments
        return ToolResult.ok("BEGIN-" + "x" * 40_000 + "-END")


class LargeOutputModel:
    def __init__(self):
        self.requests = []

    def complete(self, messages, tools):
        del tools
        self.requests.append(messages)
        if len(self.requests) == 1:
            return ModelResponse(
                content=None,
                tool_calls=(ToolCall("large-1", "large_output", "{}"),),
            )
        return ModelResponse(content="large output handled")


class MilestoneTestTool:
    name = "run_command"
    description = "Return a large successful test output."
    parameters = {"type": "object", "properties": {"argv": {"type": "array"}}}

    def run(self, arguments):
        del arguments
        return ToolResult.ok("test output\n" + "x" * 8_000, exit_code=0)


class MilestoneSummaryModel:
    def __init__(self):
        self.normal_calls = 0
        self.summary_calls = 0

    def complete(self, messages, tools):
        if not tools:
            self.summary_calls += 1
            return ModelResponse(
                content=json.dumps(
                    {
                        "goal": "validate milestone summarization",
                        "constraints": [],
                        "decisions": ["run focused tests"],
                        "completed_actions": ["focused tests passed"],
                        "modified_files": [],
                        "important_code_facts": ["test output was large"],
                        "tests": ["python3 -m unittest passed"],
                        "failed_attempts": [],
                        "blockers": [],
                        "next_action": "return the final answer",
                    }
                ),
                metadata={"usage": {"prompt_tokens": 500, "completion_tokens": 80}},
            )
        self.normal_calls += 1
        if self.normal_calls == 1:
            return ModelResponse(
                content="运行测试形成阶段检查点。",
                tool_calls=(
                    ToolCall(
                        "test-1",
                        "run_command",
                        '{"argv":["python3","-m","unittest"]}',
                    ),
                ),
            )
        return ModelResponse(content="milestone complete")


class AgentLoopTests(unittest.TestCase):
    def test_offline_model_completes_full_tool_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = []
            agent = Agent(
                ScriptedDemoModel(),
                build_registry(root, 10),
                max_steps=10,
                on_event=lambda event, payload: events.append((event, payload)),
            )
            result = agent.run("Run the demo")

            self.assertEqual(result.steps, 5)
            self.assertEqual(result.tool_calls, 4)
            self.assertTrue((root / "agent_demo.txt").is_file())
            self.assertIn("Offline demo completed", result.final_output)
            decisions = [payload for event, payload in events if event == "model_response"]
            self.assertEqual(len(decisions), 5)
            self.assertTrue(all(payload["thought"] for payload in decisions))
            self.assertTrue(all("duration_ms" in payload for payload in decisions))
            self.assertTrue(all("reasoning_content" not in payload for payload in decisions))
            tool_results = [payload for event, payload in events if event == "tool_finish"]
            self.assertTrue(all("arguments" in payload for payload in tool_results))
            self.assertTrue(all("duration_ms" in payload for payload in tool_results))

    def test_step_limit_stops_nonterminating_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            agent = Agent(
                AlwaysCallsToolModel(),
                build_registry(Path(temporary), 10),
                max_steps=3,
            )
            with self.assertRaises(AgentLimitError):
                agent.run("Never finish")

    def test_empty_model_response_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            agent = Agent(EmptyModel(), build_registry(Path(temporary), 10))
            with self.assertRaises(AgentError):
                agent.run("Do something")

    def test_cancellation_stops_before_model_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            agent = Agent(
                EmptyModel(),
                build_registry(Path(temporary), 10),
                should_stop=lambda: True,
            )
            with self.assertRaises(AgentCancelledError):
                agent.run("Do something")

    def test_agent_reuses_context_across_user_turns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            model = ContextRecordingModel()
            conversation = Conversation("system")
            agent = Agent(model, build_registry(Path(temporary), 10))

            agent.run("inspect the project", conversation=conversation)
            agent.run("continue with the same project", conversation=conversation)

            second_request = model.requests[1]
            visible_messages = [
                (message["role"], message.get("content"))
                for message in second_request
                if message["role"] in {"user", "assistant"}
            ]
            self.assertEqual(
                visible_messages,
                [
                    ("user", "inspect the project"),
                    ("assistant", "turn complete"),
                    ("user", "continue with the same project"),
                ],
            )

    def test_agent_calibrates_conversation_from_model_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            conversation = Conversation("system")
            agent = Agent(
                UsageReportingModel(),
                build_registry(Path(temporary), 10),
            )

            agent.run("record provider usage", conversation=conversation)

            stats = conversation.context_stats()
            self.assertTrue(stats["token_calibrated"])
            self.assertEqual(stats["last_prompt_tokens"], 321)

    def test_large_tool_output_is_compacted_for_model_but_full_in_event(self) -> None:
        model = LargeOutputModel()
        events = []
        agent = Agent(
            model,
            ToolRegistry([LargeOutputTool()]),
            on_event=lambda event, payload: events.append((event, payload)),
        )

        agent.run("handle a large tool result")

        tool_message = next(
            message for message in model.requests[1] if message.get("role") == "tool"
        )
        compacted = json.loads(tool_message["content"])
        compression = compacted["metadata"]["context_compression"]
        self.assertTrue(compression["truncated"])
        self.assertGreater(compression["original_chars"], 40_000)
        self.assertLess(len(tool_message["content"]), 17_000)
        self.assertIn("BEGIN-", compacted["data"])
        self.assertIn("-END", compacted["data"])
        finish = next(payload for event, payload in events if event == "tool_finish")
        self.assertGreater(len(finish["result"]), 40_000)
        self.assertTrue(finish["context_compression"]["truncated"])

    def test_large_result_has_local_reference_and_can_be_recalled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = ToolResultArchive(Path(temporary))
            registry = ToolRegistry(
                [LargeOutputTool(), ReadToolResultTool(archive)],
                result_archive=archive,
            )
            model = LargeOutputModel()
            Agent(model, registry).run("handle and archive a large result")

            tool_message = next(
                message for message in model.requests[1] if message.get("role") == "tool"
            )
            compression = json.loads(tool_message["content"])["metadata"][
                "context_compression"
            ]
            result_id = compression["result_id"]
            recalled = registry.execute(
                "read_tool_result",
                json.dumps({"result_id": result_id, "start_char": 0, "max_chars": 100}),
            )
            self.assertTrue(recalled.success)
            self.assertTrue(recalled.data.startswith('{"success": true, "data": "BEGIN-'))

    def test_high_pressure_milestone_creates_semantic_summary(self) -> None:
        model = MilestoneSummaryModel()
        events = []
        conversation = Conversation(
            "system",
            max_context_tokens=1_200,
            response_reserve_tokens=0,
        )
        agent = Agent(
            model,
            ToolRegistry([MilestoneTestTool()]),
            max_steps=4,
            on_event=lambda event, payload: events.append((event, payload)),
        )

        result = agent.run("run a focused test milestone", conversation=conversation)

        self.assertEqual(result.final_output, "milestone complete")
        self.assertEqual(model.summary_calls, 1)
        self.assertTrue(conversation.context_stats()["semantic_summary_available"])
        restored = Conversation.from_state(conversation.to_state())
        summary_layers = [
            message["content"]
            for message in restored.api_messages()
            if message.get("role") == "system" and "Lossy milestone" in message["content"]
        ]
        self.assertEqual(len(summary_layers), 1)
        self.assertIn("focused tests passed", summary_layers[0])
        self.assertIn("semantic_summary_response", {event for event, _ in events})

    def test_cli_demo_runs_without_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            exit_code = main(
                ["--demo", "--quiet", "--workspace", temporary, "Run demo"]
            )
            self.assertEqual(exit_code, 0)
            self.assertTrue((Path(temporary) / "agent_demo.txt").is_file())


if __name__ == "__main__":
    unittest.main()
