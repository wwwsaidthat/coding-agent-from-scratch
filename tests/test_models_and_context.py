from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from coding_agent.conversation import Conversation
from coding_agent.models import DeepSeekChatModel, ModelAPIError
from coding_agent.prompts import system_prompt_for_models


class ModelParsingTests(unittest.TestCase):
    def test_parse_deepseek_tool_call(self) -> None:
        response = DeepSeekChatModel._parse_response(
            {
                "id": "chat-1",
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "reasoning_content": "",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path":"main.py"}',
                                    },
                                }
                            ],
                        }
                    }
                ],
            }
        )
        self.assertEqual(response.tool_calls[0].name, "read_file")
        self.assertEqual(response.metadata["model"], "deepseek-v4-pro")

    def test_parse_rejects_malformed_response(self) -> None:
        with self.assertRaises(ModelAPIError):
            DeepSeekChatModel._parse_response({"choices": []})


class ConversationTests(unittest.TestCase):
    def test_runtime_model_identity_is_authoritative_and_refreshable(self) -> None:
        prompt = system_prompt_for_models("deepseek-v4-pro", "qwen3.6-flash")
        conversation = Conversation("old prompt")
        conversation.set_system_prompt(prompt)
        system = conversation.api_messages()[0]["content"]
        self.assertIn("primary coding model", system)
        self.assertIn("deepseek-v4-pro", system)
        self.assertIn("used only", system)
        self.assertIn("qwen3.6-flash", system)

    def test_context_trimming_keeps_tool_pairs_together(self) -> None:
        conversation = Conversation("system", max_context_chars=650)
        for index in range(4):
            conversation.start_user_turn(f"task {index}")
            conversation.add_tool_turn(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"call-{index}",
                            "type": "function",
                            "function": {"name": "x", "arguments": "{}"},
                        }
                    ],
                },
                [
                    {
                        "role": "tool",
                        "tool_call_id": f"call-{index}",
                        "content": "x" * 100,
                    }
                ],
            )
            conversation.add_final(f"finished {index}")
        messages = conversation.api_messages()
        retained_ids = {
            call["id"]
            for message in messages
            if message.get("role") == "assistant"
            for call in message.get("tool_calls", [])
        }
        tool_ids = {
            message["tool_call_id"]
            for message in messages
            if message.get("role") == "tool"
        }
        self.assertEqual(retained_ids, tool_ids)
        self.assertLess(len(retained_ids), 4)
        self.assertEqual(messages[-1]["content"], "finished 3")
        compacted = [
            message["content"]
            for message in messages
            if message.get("role") == "system" and "compacted locally" in message["content"]
        ]
        self.assertEqual(len(compacted), 1)
        self.assertIn("User request", compacted[0])

    def test_conversation_state_round_trip_and_context_stats(self) -> None:
        conversation = Conversation(
            "system",
            max_context_chars=2_000,
            project_rules="Always run focused tests.",
        )
        conversation.start_user_turn("first question")
        conversation.add_final("first answer")
        conversation.start_user_turn("follow-up")
        conversation.add_final("second answer")

        restored = Conversation.from_state(conversation.to_state())

        self.assertEqual(restored.all_messages(), conversation.all_messages())
        self.assertEqual(restored.context_stats()["total_exchanges"], 2)
        self.assertEqual(
            restored.context_stats()["project_rules_chars"],
            len("Always run focused tests."),
        )
        project_layers = [
            message["content"]
            for message in restored.api_messages()
            if message.get("role") == "system" and "Project rules" in message["content"]
        ]
        self.assertEqual(len(project_layers), 1)
        self.assertFalse(restored.has_active_turn)


if __name__ == "__main__":
    unittest.main()
