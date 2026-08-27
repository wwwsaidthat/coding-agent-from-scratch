from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from coding_agent.conversation import Conversation
from coding_agent.models import DeepSeekChatModel, ModelAPIError


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
    def test_context_trimming_keeps_tool_pairs_together(self) -> None:
        conversation = Conversation("system", "task", max_context_chars=400)
        for index in range(4):
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


if __name__ == "__main__":
    unittest.main()
