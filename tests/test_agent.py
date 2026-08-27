from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from coding_agent.agent import Agent, AgentCancelledError, AgentError, AgentLimitError
from coding_agent.cli import build_registry, main
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


class AgentLoopTests(unittest.TestCase):
    def test_offline_model_completes_full_tool_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            agent = Agent(
                ScriptedDemoModel(), build_registry(root, 10), max_steps=10
            )
            result = agent.run("Run the demo")

            self.assertEqual(result.steps, 4)
            self.assertEqual(result.tool_calls, 3)
            self.assertTrue((root / "agent_demo.txt").is_file())
            self.assertIn("Offline demo completed", result.final_output)

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

    def test_cli_demo_runs_without_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            exit_code = main(
                ["--demo", "--quiet", "--workspace", temporary, "Run demo"]
            )
            self.assertEqual(exit_code, 0)
            self.assertTrue((Path(temporary) / "agent_demo.txt").is_file())


if __name__ == "__main__":
    unittest.main()
