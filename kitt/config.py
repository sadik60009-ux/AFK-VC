"""Environment configuration loading and validation."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field


class ConfigError(Exception):
    """Raised when the environment configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class Config:
    """Validated runtime configuration."""

    # repr=False so the token can never leak through logging or tracebacks.
    token: str = field(repr=False)
    guild_id: int
    voice_channel_id: int


def _parse_snowflake(name: str, raw: str | None, problems: list[str]) -> int:
    # Values are deliberately never echoed back: a user who pasted their bot
    # token into the wrong variable must not see it end up in the logs.
    if raw is None or not raw.strip():
        problems.append(f"{name} is missing or empty.")
        return 0
    try:
        value = int(raw.strip())
    except ValueError:
        problems.append(f"{name} must be a numeric Discord ID (digits only).")
        return 0
    if value <= 0:
        problems.append(f"{name} must be a positive Discord ID.")
        return 0
    return value


def load_config(environ: Mapping[str, str] | None = None) -> Config:
    """Read and validate configuration, reporting every problem at once."""
    env = os.environ if environ is None else environ
    problems: list[str] = []

    token = (env.get("DISCORD_TOKEN") or "").strip()
    if not token:
        problems.append("DISCORD_TOKEN is missing or empty.")
    elif any(ch.isspace() for ch in token):
        problems.append("DISCORD_TOKEN must not contain whitespace.")

    guild_id = _parse_snowflake("GUILD_ID", env.get("GUILD_ID"), problems)
    channel_id = _parse_snowflake("VOICE_CHANNEL_ID", env.get("VOICE_CHANNEL_ID"), problems)

    if problems:
        raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(problems))
    return Config(token=token, guild_id=guild_id, voice_channel_id=channel_id)
