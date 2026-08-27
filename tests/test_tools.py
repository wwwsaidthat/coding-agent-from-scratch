from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from coding_agent.tools.base import ToolExecutionError
from coding_agent.tools.filesystem import (
    ListFilesTool,
    ReadFileTool,
    ReplaceInFileTool,
    WorkspacePaths,
    WriteFileTool,
)
from coding_agent.tools.registry import ToolRegistry
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
        listing = ListFilesTool(self.paths).run({})
        self.assertNotIn(".env", listing.data)
        with self.assertRaises(ToolExecutionError) as caught:
            ReadFileTool(self.paths).run({"path": ".env"})
        self.assertEqual(caught.exception.code, "SensitivePath")

    def test_replace_requires_exact_match_count(self) -> None:
        target = self.root / "values.txt"
        target.write_text("x\nx\n", encoding="utf-8")
        with self.assertRaises(ToolExecutionError) as caught:
            ReplaceInFileTool(self.paths).run(
                {"path": "values.txt", "old_text": "x", "new_text": "y"}
            )
        self.assertEqual(caught.exception.code, "ReplacementCountMismatch")
        self.assertEqual(target.read_text(), "x\nx\n")


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
