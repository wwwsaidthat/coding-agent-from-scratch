from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from coding_agent.tools.base import ToolExecutionError
from coding_agent.cli import build_registry
from coding_agent.config import Settings
from coding_agent.tools.external import AnalyzeImageTool, AnalyzePdfTool, WebSearchTool
from coding_agent.tools.filesystem import (
    ListFilesTool,
    MultiEditTool,
    ReadFileTool,
    ReplaceInFileTool,
    WorkspacePaths,
    WriteFileTool,
)
from coding_agent.tools.registry import ToolRegistry
from coding_agent.tools.planning import UpdatePlanTool
from coding_agent.tools.search import FindFilesTool, SearchCodeTool
from coding_agent.tools.shell import RunCommandTool


class FilesystemToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.paths = WorkspacePaths(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_write_read_and_replace_round_trip(self) -> None:
        writer = WriteFileTool(self.paths)
        reader = ReadFileTool(self.paths)
        replacer = ReplaceInFileTool(self.paths)

        written = writer.run({"path": "src/app.py", "content": "value = 1\n"})
        self.assertTrue(written.success)

        read = reader.run({"path": "src/app.py"})
        self.assertIn("value = 1", read.data)

        replaced = replacer.run(
            {"path": "src/app.py", "old_text": "value = 1", "new_text": "value = 2"}
        )
        self.assertTrue(replaced.success)
        self.assertEqual((self.root / "src/app.py").read_text(), "value = 2\n")

    def test_existing_file_requires_overwrite(self) -> None:
        target = self.root / "file.txt"
        target.write_text("old", encoding="utf-8")
        result = WriteFileTool(self.paths)
        with self.assertRaises(ToolExecutionError) as caught:
            result.run({"path": "file.txt", "content": "new"})
        self.assertEqual(caught.exception.code, "AlreadyExists")
        self.assertEqual(target.read_text(), "old")

    def test_path_traversal_is_denied(self) -> None:
        with self.assertRaises(ToolExecutionError) as caught:
            self.paths.resolve("../outside.txt", allow_missing=True)
        self.assertEqual(caught.exception.code, "PathDenied")

    def test_missing_file_has_structured_error(self) -> None:
        with self.assertRaises(ToolExecutionError) as caught:
            ReadFileTool(self.paths).run({"path": "missing.txt"})
        self.assertEqual(caught.exception.code, "NotFile")

    def test_sensitive_files_are_hidden_and_denied(self) -> None:
        (self.root / ".env").write_text("SECRET=value", encoding="utf-8")
        session_dir = self.root / ".coding-agent"
        session_dir.mkdir()
        (session_dir / "session.json").write_text("{}", encoding="utf-8")
        listing = ListFilesTool(self.paths).run({})
        self.assertNotIn(".env", listing.data)
        self.assertNotIn(".coding-agent", listing.data)
        with self.assertRaises(ToolExecutionError) as caught:
            ReadFileTool(self.paths).run({"path": ".env"})
        self.assertEqual(caught.exception.code, "SensitivePath")
        with self.assertRaises(ToolExecutionError) as caught:
            ReadFileTool(self.paths).run({"path": ".coding-agent/session.json"})
        self.assertEqual(caught.exception.code, "SensitivePath")

    def test_replace_requires_exact_match_count(self) -> None:
        target = self.root / "values.txt"
        target.write_text("x\nx\n", encoding="utf-8")
        ReadFileTool(self.paths).run({"path": "values.txt"})
        with self.assertRaises(ToolExecutionError) as caught:
            ReplaceInFileTool(self.paths).run(
                {"path": "values.txt", "old_text": "x", "new_text": "y"}
            )
        self.assertEqual(caught.exception.code, "ReplacementCountMismatch")
        self.assertEqual(target.read_text(), "x\nx\n")

    def test_existing_file_must_be_read_before_overwrite(self) -> None:
        target = self.root / "file.txt"
        target.write_text("old", encoding="utf-8")
        with self.assertRaises(ToolExecutionError) as caught:
            WriteFileTool(self.paths).run(
                {"path": "file.txt", "content": "new", "overwrite": True}
            )
        self.assertEqual(caught.exception.code, "ReadRequired")
        self.assertEqual(target.read_text(), "old")

    def test_external_change_after_read_returns_conflict(self) -> None:
        target = self.root / "file.txt"
        target.write_text("old", encoding="utf-8")
        ReadFileTool(self.paths).run({"path": "file.txt"})
        target.write_text("changed elsewhere", encoding="utf-8")
        with self.assertRaises(ToolExecutionError) as caught:
            ReplaceInFileTool(self.paths).run(
                {"path": "file.txt", "old_text": "old", "new_text": "new"}
            )
        self.assertEqual(caught.exception.code, "Conflict")
        self.assertEqual(target.read_text(), "changed elsewhere")

    def test_rejected_diff_does_not_write_file(self) -> None:
        proposals = []
        writer = WriteFileTool(
            self.paths,
            approval_handler=lambda proposal: proposals.append(proposal) or False,
        )
        with self.assertRaises(ToolExecutionError) as caught:
            writer.run({"path": "new.py", "content": "answer = 42\n"})
        self.assertEqual(caught.exception.code, "EditRejected")
        self.assertFalse((self.root / "new.py").exists())
        self.assertIn("+answer = 42", proposals[0]["files"][0]["diff"])

    def test_multi_edit_validates_then_commits_all_files(self) -> None:
        first = self.root / "first.py"
        second = self.root / "second.py"
        first.write_text("a = 1\n", encoding="utf-8")
        second.write_text("b = 2\n", encoding="utf-8")
        reader = ReadFileTool(self.paths)
        reader.run({"path": "first.py"})
        reader.run({"path": "second.py"})
        proposals = []
        result = MultiEditTool(
            self.paths,
            approval_handler=lambda proposal: proposals.append(proposal) or True,
        ).run(
            {
                "edits": [
                    {"path": "first.py", "old_text": "1", "new_text": "10"},
                    {"path": "second.py", "old_text": "2", "new_text": "20"},
                ]
            }
        )
        self.assertTrue(result.success)
        self.assertEqual(first.read_text(), "a = 10\n")
        self.assertEqual(second.read_text(), "b = 20\n")
        self.assertEqual(len(proposals[0]["files"]), 2)

    def test_find_files_and_search_code_return_structured_results(self) -> None:
        source = self.root / "src"
        source.mkdir()
        (source / "alpha.py").write_text("needle = 1\n", encoding="utf-8")
        (source / "beta.txt").write_text("needle = 2\n", encoding="utf-8")

        found = FindFilesTool(self.paths).run(
            {"glob": "*.py", "path": "src", "max_results": 10}
        )
        self.assertEqual(found.data["files"], ["src/alpha.py"])
        self.assertFalse(found.data["truncated"])

        searched = SearchCodeTool(self.paths).run(
            {"query": "needle", "path": "src", "glob": "*.py"}
        )
        self.assertEqual(searched.data["count"], 1)
        self.assertEqual(searched.data["matches"][0]["path"], "src/alpha.py")
        self.assertEqual(searched.data["matches"][0]["line"], 1)


class FakeQwenClient:
    api_key = "test-only"
    provider = "qwen.test"
    model = "qwen3.6-flash"

    def __init__(self) -> None:
        self.payloads = []

    def require_key(self) -> None:
        return None

    def create(self, payload):
        self.payloads.append(payload)
        return {
            "model": self.model,
            "choices": [{"message": {"content": "Qwen result"}}],
            "usage": {"total_tokens": 9},
        }

    @staticmethod
    def output_text(data):
        return data["choices"][0]["message"]["content"]

    @staticmethod
    def sources(data):
        return []


class ExternalAndPlanningToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.paths = WorkspacePaths(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_web_search_requires_approval_and_enables_qwen_search(self) -> None:
        client = FakeQwenClient()
        proposals = []
        result = WebSearchTool(
            client, lambda proposal: proposals.append(proposal) or True
        ).run({"query": "latest Python release", "search_depth": "high"})
        self.assertTrue(result.success)
        self.assertEqual(proposals[0]["kind"], "external")
        self.assertTrue(client.payloads[0]["enable_search"])
        self.assertEqual(client.payloads[0]["model"], "qwen3.6-flash")

    def test_rejected_image_is_never_sent_to_qwen(self) -> None:
        image = self.root / "sample.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"test")
        client = FakeQwenClient()
        proposals = []
        with self.assertRaises(ToolExecutionError) as caught:
            AnalyzeImageTool(
                self.paths,
                client,
                lambda proposal: proposals.append(proposal) or False,
            ).run({"path": "sample.png", "prompt": "describe it"})
        self.assertEqual(caught.exception.code, "ExternalActionRejected")
        self.assertFalse(client.payloads)
        self.assertEqual(proposals[0]["details"]["path"], "sample.png")
        self.assertIn("sha256", proposals[0]["details"])

    def test_approved_image_uses_qwen_vision_message(self) -> None:
        image = self.root / "sample.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"test")
        client = FakeQwenClient()
        result = AnalyzeImageTool(self.paths, client, lambda proposal: True).run(
            {"path": "sample.png", "prompt": "describe it"}
        )
        self.assertTrue(result.success)
        content = client.payloads[0]["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "image_url")
        self.assertTrue(content[0]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_approved_pdf_renders_ordered_pages_for_qwen(self) -> None:
        pdf = self.root / "sample.pdf"
        pdf.write_bytes(b"%PDF-1.7\nlocal-test")
        client = FakeQwenClient()
        proposals = []
        with (
            patch("coding_agent.tools.external._pdf_page_count", return_value=2),
            patch(
                "coding_agent.tools.external._render_pdf_pages",
                return_value=[b"\xff\xd8\xffpage-one", b"\xff\xd8\xffpage-two"],
            ),
        ):
            result = AnalyzePdfTool(
                self.paths,
                client,
                lambda proposal: proposals.append(proposal) or True,
            ).run({"path": "sample.pdf", "prompt": "summarize it"})

        self.assertTrue(result.success)
        self.assertEqual(result.data["page_count"], 2)
        self.assertEqual(proposals[0]["details"]["page_count"], 2)
        content = client.payloads[0]["messages"][0]["content"]
        page_images = [item for item in content if item["type"] == "image_url"]
        self.assertEqual(len(page_images), 2)
        self.assertTrue(
            all(
                item["image_url"]["url"].startswith("data:image/jpeg;base64,")
                for item in page_images
            )
        )
        self.assertIn("summarize it", content[-1]["text"])

    def test_rejected_pdf_is_not_rendered_or_sent_to_qwen(self) -> None:
        pdf = self.root / "sample.pdf"
        pdf.write_bytes(b"%PDF-1.7\nlocal-test")
        client = FakeQwenClient()
        with (
            patch("coding_agent.tools.external._pdf_page_count", return_value=1),
            patch("coding_agent.tools.external._render_pdf_pages") as render,
            self.assertRaises(ToolExecutionError) as caught,
        ):
            AnalyzePdfTool(self.paths, client, lambda proposal: False).run(
                {"path": "sample.pdf", "prompt": "summarize it"}
            )
        self.assertEqual(caught.exception.code, "ExternalActionRejected")
        render.assert_not_called()
        self.assertFalse(client.payloads)

    def test_plan_validation_and_callback(self) -> None:
        stored = []
        tool = UpdatePlanTool(lambda payload: stored.append(payload) or payload)
        result = tool.run(
            {
                "explanation": "start",
                "plan": [
                    {"step": "inspect", "status": "completed"},
                    {"step": "implement", "status": "in_progress"},
                ],
            }
        )
        self.assertTrue(result.success)
        self.assertEqual(stored[0]["plan"][0]["status"], "completed")
        with self.assertRaises(ToolExecutionError):
            tool.run(
                {
                    "plan": [
                        {"step": "one", "status": "in_progress"},
                        {"step": "two", "status": "in_progress"},
                    ]
                }
            )


class RegistryAndCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.paths = WorkspacePaths(self.root)
        self.command = RunCommandTool(self.paths)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_registry_reports_invalid_json(self) -> None:
        registry = ToolRegistry([ListFilesTool(self.paths)])
        result = registry.execute("list_files", "not-json")
        self.assertFalse(result.success)
        self.assertEqual(result.metadata["code"], "InvalidJSON")

    def test_configured_registry_exposes_pdf_analysis(self) -> None:
        settings = Settings(
            api_key="test-only",
            qwen_api_key="test-only",
            qwen_base_url="https://qwen.test/v1",
        )
        registry = build_registry(self.root, 5, settings=settings)
        names = [definition["function"]["name"] for definition in registry.definitions]
        self.assertIn("analyze_pdf", names)

    def test_allowed_command_runs_without_shell(self) -> None:
        result = self.command.run({"argv": ["python3", "--version"]})
        self.assertTrue(result.success)
        self.assertEqual(result.metadata["exit_code"], 0)

    def test_nonzero_exit_is_a_failed_tool_result(self) -> None:
        result = self.command.run(
            {"argv": ["python3", "-m", "module_that_should_not_exist_12345"]}
        )
        self.assertFalse(result.success)
        self.assertNotEqual(result.metadata["exit_code"], 0)

    def test_remote_git_push_is_denied(self) -> None:
        with self.assertRaises(ToolExecutionError) as caught:
            self.command.run({"argv": ["git", "push"]})
        self.assertEqual(caught.exception.code, "CommandDenied")


if __name__ == "__main__":
    unittest.main()
