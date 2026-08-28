"""Environment-backed application configuration."""

from __future__ import annotations

from dataclasses import dataclass, replace
import os
from pathlib import Path
import re


ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_env_file(path: Path) -> None:
    """Load a small KEY=VALUE file without printing or overwriting shell values."""
    if not path.is_file():
        return
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"Invalid .env entry on line {line_number}")
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not ENV_NAME.fullmatch(name):
            raise ValueError(f"Invalid environment name on line {line_number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(name, value)


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings. Secrets are read only from environment variables."""

    api_key: str | None
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-pro"
    thinking: str = "enabled"
    reasoning_effort: str = "high"
    max_steps: int = 20
    api_timeout: int = 90
    command_timeout: int = 30
    qwen_api_key: str | None = None
    qwen_base_url: str = (
        "https://YOUR_WORKSPACE_ID.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    )
    qwen_model: str = "qwen3.6-flash"

    @classmethod
    def from_env(cls) -> "Settings":
        thinking = os.getenv("DEEPSEEK_THINKING", "enabled").lower()
        if thinking not in {"enabled", "disabled"}:
            raise ValueError("DEEPSEEK_THINKING must be 'enabled' or 'disabled'")

        reasoning_effort = os.getenv("DEEPSEEK_REASONING_EFFORT", "high").lower()
        if reasoning_effort not in {"low", "high", "max"}:
            raise ValueError(
                "DEEPSEEK_REASONING_EFFORT must be 'low', 'high', or 'max'"
            )

        return cls(
            api_key=os.getenv("DEEPSEEK_API_KEY"),
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro"),
            thinking=thinking,
            reasoning_effort=reasoning_effort,
            max_steps=_positive_int("CODING_AGENT_MAX_STEPS", 20),
            api_timeout=_positive_int("CODING_AGENT_API_TIMEOUT", 90),
            command_timeout=_positive_int("CODING_AGENT_COMMAND_TIMEOUT", 30),
            qwen_api_key=os.getenv("QWEN_API_KEY"),
            qwen_base_url=os.getenv(
                "QWEN_BASE_URL",
                "https://YOUR_WORKSPACE_ID.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            ),
            qwen_model=os.getenv("QWEN_MODEL", "qwen3.6-flash"),
        )

    def require_api_key(self) -> "Settings":
        if not self.api_key:
            raise ValueError(
                "DEEPSEEK_API_KEY is not set. Export it in your shell before running."
            )
        return self

    def with_max_steps(self, max_steps: int | None) -> "Settings":
        if max_steps is None:
            return self
        if max_steps <= 0:
            raise ValueError("max_steps must be greater than zero")
        return replace(self, max_steps=max_steps)
