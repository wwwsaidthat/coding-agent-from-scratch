from pathlib import Path
import json
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from coding_agent.conversation import Conversation, MemoryCheckpoint
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
    @staticmethod
    def _tool_call(call_id: str, name: str, arguments: dict) -> dict:
        return {
            "id": call_id,
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(arguments, ensure_ascii=False),
            },
        }

    @staticmethod
    def _tool_message(call_id: str, payload: dict) -> dict:
        return {
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps(payload, ensure_ascii=False),
        }

    def test_runtime_model_identity_is_authoritative_and_refreshable(self) -> None:
        prompt = system_prompt_for_models("deepseek-v4-pro", "qwen3.6-flash")
        conversation = Conversation("old prompt")
        conversation.set_system_prompt(prompt)
        system = conversation.api_messages()[0]["content"]
        self.assertIn("15. Treat the following runtime model identity", system)
        self.assertIn("primary coding model", system)
        self.assertIn("deepseek-v4-pro", system)
        self.assertIn("used only", system)
        self.assertIn("qwen3.6-flash", system)

    def test_context_trimming_keeps_tool_pairs_together(self) -> None:
        conversation = Conversation(
            "system",
            max_context_chars=650,
            max_context_tokens=300,
            response_reserve_tokens=0,
        )
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

    def test_long_exchange_compacts_whole_tool_rounds_and_keeps_recent_two(self) -> None:
        conversation = Conversation(
            "system",
            max_context_tokens=900,
            response_reserve_tokens=0,
        )
        conversation.start_user_turn("完成一个包含多步工具调用的任务")
        for index in range(6):
            call = self._tool_call(
                f"call-{index}", "read_file", {"path": f"file-{index}.py"}
            )
            conversation.add_tool_turn(
                {"role": "assistant", "content": None, "tool_calls": [call]},
                [
                    self._tool_message(
                        f"call-{index}",
                        {"success": True, "data": "x" * 1_000},
                    )
                ],
            )
        conversation.add_final("任务完成")

        messages = conversation.api_messages()
        retained_call_ids = [
            call["id"]
            for message in messages
            if message.get("role") == "assistant"
            for call in message.get("tool_calls", [])
        ]
        retained_tool_ids = [
            message["tool_call_id"]
            for message in messages
            if message.get("role") == "tool"
        ]
        self.assertEqual(retained_call_ids, retained_tool_ids)
        self.assertEqual(retained_call_ids, ["call-4", "call-5"])
        summaries = [
            message["content"]
            for message in messages
            if message.get("role") == "system"
            and "earlier tool round(s)" in message["content"]
        ]
        self.assertEqual(len(summaries), 1)
        self.assertIn("tools=read_file", summaries[0])
        stats = conversation.context_stats()
        self.assertEqual(stats["compacted_tool_rounds"], 4)
        self.assertEqual(stats["retained_tool_rounds"], 2)

    def test_seventy_percent_tier_compacts_old_outputs_before_dropping_rounds(self) -> None:
        conversation = Conversation(
            "system",
            max_context_tokens=13_000,
            response_reserve_tokens=0,
        )
        conversation.start_user_turn("inspect several large files")
        for index in range(6):
            call = self._tool_call(
                f"call-{index}", "read_file", {"path": f"file-{index}.py"}
            )
            conversation.add_tool_turn(
                {"role": "assistant", "content": None, "tool_calls": [call]},
                [
                    self._tool_message(
                        f"call-{index}",
                        {"success": True, "data": "line\n" * 800},
                    )
                ],
            )
        conversation.add_final("inspection complete")

        stats = conversation.context_stats()
        self.assertEqual(stats["context_tier"], "deterministic_cleanup")
        self.assertGreater(stats["compacted_tool_outputs"], 0)
        self.assertEqual(stats["dropped_exchanges"], 0)
        messages = conversation.api_messages()
        call_ids = {
            call["id"]
            for message in messages
            for call in message.get("tool_calls", [])
        }
        result_ids = {
            message["tool_call_id"]
            for message in messages
            if message.get("role") == "tool"
        }
        self.assertEqual(call_ids, result_ids)

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

    def test_structured_checkpoint_tracks_task_edits_plan_and_tests(self) -> None:
        conversation = Conversation("system")
        conversation.start_user_turn(
            "请修改登录逻辑。必须先读文件，每次修改后需要运行测试。"
        )
        calls = [
            self._tool_call(
                "edit-1",
                "replace_in_file",
                {"path": "src/login.py", "old_text": "old", "new_text": "new"},
            ),
            self._tool_call(
                "plan-1",
                "update_plan",
                {
                    "explanation": "登录修复完成后验证回归测试",
                    "plan": [
                        {"step": "修复登录逻辑", "status": "completed"},
                        {"step": "运行完整测试", "status": "in_progress"},
                    ],
                },
            ),
            self._tool_call(
                "test-1",
                "run_command",
                {"argv": ["python3", "-m", "unittest"]},
            ),
        ]
        conversation.add_tool_turn(
            {"role": "assistant", "content": None, "tool_calls": calls},
            [
                self._tool_message(
                    "edit-1",
                    {"success": True, "data": "Updated src/login.py"},
                ),
                self._tool_message(
                    "plan-1",
                    {
                        "success": True,
                        "data": {
                            "explanation": "登录修复完成后验证回归测试",
                            "plan": [
                                {"step": "修复登录逻辑", "status": "completed"},
                                {"step": "运行完整测试", "status": "in_progress"},
                            ],
                        },
                    },
                ),
                self._tool_message(
                    "test-1",
                    {
                        "success": True,
                        "data": "OK",
                        "metadata": {"exit_code": 0},
                    },
                ),
            ],
        )
        conversation.add_final("登录逻辑已修复，测试通过。")

        memory = conversation.memory_checkpoint()
        self.assertIn("登录逻辑", memory["goal"])
        self.assertTrue(any("必须先读文件" in item for item in memory["constraints"]))
        self.assertIn("src/login.py", memory["modified_files"])
        self.assertIn("修复登录逻辑", memory["completed"])
        self.assertIn("运行完整测试", memory["next_steps"])
        self.assertTrue(any("passed" in item for item in memory["tests"]))
        self.assertIn("登录修复完成后验证回归测试", memory["decisions"])
        checkpoint_layers = [
            message["content"]
            for message in conversation.api_messages()
            if message.get("role") == "system"
            and "Structured task memory checkpoint" in message["content"]
        ]
        self.assertEqual(len(checkpoint_layers), 1)
        self.assertIn("src/login.py", checkpoint_layers[0])

    def test_checkpoint_records_failures_and_survives_round_trip(self) -> None:
        conversation = Conversation("system")
        conversation.start_user_turn("修改 app.py")
        call = self._tool_call(
            "edit-1",
            "replace_in_file",
            {"path": "app.py", "old_text": "a", "new_text": "b"},
        )
        conversation.add_tool_turn(
            {"role": "assistant", "content": None, "tool_calls": [call]},
            [
                self._tool_message(
                    "edit-1",
                    {
                        "success": False,
                        "error": "File changed after it was read",
                        "metadata": {"code": "Conflict"},
                    },
                )
            ],
        )
        conversation.add_final("检测到文件冲突，未修改。")

        restored = Conversation.from_state(conversation.to_state())
        self.assertEqual(restored.memory_checkpoint(), conversation.memory_checkpoint())
        self.assertTrue(restored.memory_checkpoint()["failed_attempts"])
        self.assertTrue(restored.memory_checkpoint()["do_not_repeat"])
        self.assertGreater(restored.context_stats()["memory_checkpoint_items"], 0)

    def test_old_conversation_state_migrates_with_empty_checkpoint(self) -> None:
        legacy = {
            "system_prompt": "system",
            "project_rules": "",
            "max_context_chars": 2_000,
            "exchanges": [[{"role": "user", "content": "old request"}, {"role": "assistant", "content": "done"}]],
            "active": False,
        }
        restored = Conversation.from_state(legacy)
        memory = restored.memory_checkpoint()
        self.assertEqual(memory["goal"], "")
        self.assertTrue(all(not value for key, value in memory.items() if key != "goal"))

    def test_token_budget_calibrates_from_provider_usage_and_persists(self) -> None:
        conversation = Conversation(
            "system",
            max_context_tokens=2_000,
            response_reserve_tokens=200,
        )
        conversation.set_tool_definitions(
            [{"type": "function", "function": {"name": "read_file"}}]
        )
        conversation.start_user_turn("检查项目并说明结构")
        request = conversation.api_messages()
        before = conversation.context_stats()
        self.assertFalse(before["token_calibrated"])
        self.assertEqual(before["budget_tokens"], 1_740)

        conversation.observe_usage(request, {"prompt_tokens": 900})
        after = conversation.context_stats()
        self.assertTrue(after["token_calibrated"])
        self.assertEqual(after["token_calibration_samples"], 1)
        self.assertEqual(after["last_prompt_tokens"], 900)
        self.assertGreater(after["estimated_tokens"], before["estimated_tokens"])

        restored = Conversation.from_state(conversation.to_state())
        restored_stats = restored.context_stats()
        self.assertTrue(restored_stats["token_calibrated"])
        self.assertEqual(restored_stats["last_prompt_tokens"], 900)
        self.assertEqual(restored_stats["max_context_tokens"], 2_000)

    def test_memory_and_token_state_are_bounded_during_restore(self) -> None:
        checkpoint = MemoryCheckpoint.from_state(
            {
                "goal": "目" * 3_000,
                "constraints": [f"constraint-{index}-" + "x" * 800 for index in range(40)],
            }
        )
        self.assertEqual(len(checkpoint.goal), 2_000)
        self.assertEqual(len(checkpoint.constraints), 24)
        self.assertTrue(all(len(item) <= 600 for item in checkpoint.constraints))

        state = {
            "system_prompt": "system",
            "project_rules": "",
            "max_context_chars": 2_000,
            "max_context_tokens": 200,
            "response_reserve_tokens": 9_999,
            "token_calibration": {"factor": 99, "samples": -2},
            "exchanges": [
                [
                    {"role": "user", "content": "request"},
                    {"role": "assistant", "content": "done"},
                ]
            ],
            "active": False,
        }
        restored = Conversation.from_state(state)
        stats = restored.context_stats()
        self.assertEqual(stats["max_context_tokens"], 200)
        self.assertLess(stats["response_reserve_tokens"], 200)
        self.assertFalse(stats["token_calibrated"])


if __name__ == "__main__":
    unittest.main()
