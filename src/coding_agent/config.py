"""Environment-backed application configuration."""

from __future__ import annotations

from dataclasses import dataclass, replace
import os


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
