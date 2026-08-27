"""Run the coding agent directly from a source checkout."""

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from coding_agent.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
