from pathlib import Path
import json
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from coding_agent.context_compression import compress_tool_result
from coding_agent.tools.result_archive import ReadToolResultTool, ToolResultArchive


class ToolAwareCompressionTests(unittest.TestCase):
    def test_search_code_preserves_structured_match_fields(self) -> None:
        matches = [
            {
                "path": f"src/module_{index}.py",
                "line": index + 1,
                "column": 3,
                "text": "important_call(" + "x" * 180 + ")",
            }
            for index in range(100)
        ]
        original = json.dumps(
            {"success": True, "data": {"matches": matches, "count": 100}},
            ensure_ascii=False,
        )

        compressed_json, metadata = compress_tool_result(
            "search_code", original, limit=4_000
        )
        compressed = json.loads(compressed_json)

        self.assertEqual(metadata["strategy"], "structured_matches")
        self.assertTrue(metadata["truncated"])
        self.assertLess(compressed["data"]["returned_count"], 100)
        first = compressed["data"]["matches"][0]
        self.assertEqual(set(first), {"path", "line", "column", "text"})

    def test_run_command_keeps_diagnostics_and_final_summary(self) -> None:
        output = "\n".join(
            [*(f"progress {index}" for index in range(500)), "AssertionError: expected 2", "3 failed, 40 passed"]
        )
        original = json.dumps(
            {
                "success": False,
                "error": output,
                "metadata": {"exit_code": 1},
            }
        )

        compressed_json, metadata = compress_tool_result(
            "run_command", original, limit=2_000
        )
        compressed = json.loads(compressed_json)

        self.assertEqual(metadata["strategy"], "diagnostic_lines")
        self.assertIn("AssertionError", compressed["error"])
        self.assertIn("3 failed, 40 passed", compressed["error"])
        self.assertEqual(compressed["metadata"]["exit_code"], 1)

    def test_web_search_keeps_source_objects(self) -> None:
        original = json.dumps(
            {
                "success": True,
                "data": {
                    "answer": "a" * 20_000,
                    "sources": [
                        {"title": "Python", "url": "https://docs.python.org/3/"}
                    ],
                },
            }
        )
        compressed_json, metadata = compress_tool_result(
            "web_search", original, limit=4_000
        )
        compressed = json.loads(compressed_json)
        self.assertEqual(metadata["strategy"], "answer_and_sources")
        self.assertEqual(
            compressed["data"]["sources"][0]["url"],
            "https://docs.python.org/3/",
        )

    def test_generic_json_preserves_field_structure(self) -> None:
        original = json.dumps(
            {
                "success": True,
                "data": {
                    "status": "ready",
                    "details": {"message": "x" * 12_000, "code": 7},
                    "items": [{"id": index, "value": "y" * 100} for index in range(80)],
                },
            }
        )

        compressed_json, metadata = compress_tool_result(
            "custom_json_tool", original, limit=3_000
        )
        compressed = json.loads(compressed_json)

        self.assertEqual(metadata["strategy"], "structured_json")
        self.assertEqual(compressed["data"]["status"], "ready")
        self.assertEqual(compressed["data"]["details"]["code"], 7)
        self.assertIsInstance(compressed["data"]["items"], list)


class ToolResultArchiveTests(unittest.TestCase):
    def test_archived_result_can_be_read_in_verified_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = ToolResultArchive(Path(temporary))
            content = "BEGIN-" + "x" * 30_000 + "-END"
            reference = archive.store("run_command", content)
            tool = ReadToolResultTool(archive)

            first = tool.run(
                {
                    "result_id": reference["result_id"],
                    "start_char": 0,
                    "max_chars": 100,
                }
            )
            last = tool.run(
                {
                    "result_id": reference["result_id"],
                    "start_char": len(content) - 100,
                    "max_chars": 100,
                }
            )

            self.assertTrue(first.data.startswith("BEGIN-"))
            self.assertTrue(first.metadata["truncated"])
            self.assertTrue(last.data.endswith("-END"))
            self.assertEqual(first.metadata["sha256"], reference["sha256"])
