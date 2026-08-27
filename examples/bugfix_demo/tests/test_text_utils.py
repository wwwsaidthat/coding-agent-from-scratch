from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from text_utils import slugify


class SlugifyTests(unittest.TestCase):
    def test_simple_words(self) -> None:
        self.assertEqual(slugify("Hello World"), "hello-world")

    def test_collapses_spaces_and_trims_separator(self) -> None:
        self.assertEqual(slugify("  Build   Better Agents  "), "build-better-agents")

    def test_removes_punctuation_and_normalizes_underscores(self) -> None:
        self.assertEqual(slugify("DeepSeek_V4: Pro!"), "deepseek-v4-pro")

    def test_avoids_repeated_hyphens(self) -> None:
        self.assertEqual(slugify("safe---tool calling"), "safe-tool-calling")


if __name__ == "__main__":
    unittest.main()
