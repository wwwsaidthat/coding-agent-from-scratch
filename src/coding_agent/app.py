"""Top-level application entry point and runtime-mode dispatch."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Sequence

from .agent import AgentError
from .cli import run_cli
from .config import Settings, load_env_file
from .models import ModelAPIError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coding-agent",
        description="A lightweight coding agent built without an agent framework.",
    )
    parser.add_argument("task", nargs="*", help="Programming task for the agent")
    parser.add_argument(
        "--workspace",
        default=".",
        help="Directory the agent may access (default: current directory)",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=None,
        help="Maximum model turns before stopping",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run a deterministic offline tool-loop demo without an API key",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Hide intermediate model and tool events",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Start the local visual web interface",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Web server host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Web server port (default: 8765)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Load shared configuration and dispatch to the selected user interface."""
    args = build_parser().parse_args(argv)
    try:
        load_env_file(Path.cwd() / ".env")
        settings = Settings.from_env().with_max_steps(args.max_steps)
        workspace = Path(args.workspace).expanduser().resolve()
        if args.web:
            from .webapp import run_web_server

            return run_web_server(
                settings,
                host=args.host,
                port=args.port,
                default_workspace=workspace,
            )
        return run_cli(
            settings,
            workspace,
            task=" ".join(args.task).strip(),
            demo=args.demo,
            quiet=args.quiet,
        )
    except (ValueError, AgentError, ModelAPIError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
