from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from coding_agent.agent import Agent, AgentCancelledError, AgentError, AgentLimitError
from coding_agent.cli import build_registry, main
from coding_agent.conversation import Conversation
from coding_agent.models import ModelResponse, ScriptedDemoModel, ToolCall


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

    def test_cli_demo_runs_without_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            exit_code = main(
                ["--demo", "--quiet", "--workspace", temporary, "Run demo"]
            )
            self.assertEqual(exit_code, 0)
            self.assertTrue((Path(temporary) / "agent_demo.txt").is_file())


if __name__ == "__main__":
    unittest.main()
