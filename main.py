"""KITT - minimal Discord voice-presence bot.

Stays connected to a single configured voice channel, silently, and
recovers from recoverable failures (drops, gateway reconnects, restarts).
"""

import sys

if sys.version_info < (3, 10):
    sys.exit("KITT requires Python 3.10 or newer.")

import asyncio
import contextlib
import importlib.util
import logging
import signal

import discord
from dotenv import load_dotenv

from kitt.bot import KittBot
from kitt.config import Config, ConfigError, load_config

log = logging.getLogger("kitt")

REQUIRED_VOICE_MODULES = {"nacl": "PyNaCl", "davey": "davey"}


class RedactingFilter(logging.Filter):
    """Replaces registered secrets (the bot token) in every log record."""

    def __init__(self) -> None:
        super().__init__()
        self._secrets: list[str] = []

    def add_secret(self, secret: str) -> None:
        if secret:
            self._secrets.append(secret)

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "[REDACTED]")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        message = record.getMessage()
        redacted = self._redact(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self._redact(record.exc_text)
        return True


def configure_logging() -> RedactingFilter:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    redactor = RedactingFilter()
    handler.addFilter(redactor)
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    return redactor


def missing_voice_dependencies() -> list[str]:
    return [pip for module, pip in REQUIRED_VOICE_MODULES.items() if importlib.util.find_spec(module) is None]


async def run_bot(config: Config) -> None:
    bot = KittBot(config)
    loop = asyncio.get_running_loop()
    pending: set[asyncio.Task[None]] = set()

    def request_shutdown(sig_name: str) -> None:
        log.info("Received %s; shutting down...", sig_name)
        task = loop.create_task(bot.close())
        pending.add(task)
        task.add_done_callback(pending.discard)

    # Railway stops containers with SIGTERM; asyncio only handles SIGINT by default.
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):  # e.g. Windows
            loop.add_signal_handler(sig, request_shutdown, sig.name)

    async with bot:
        await bot.start(config.token, reconnect=True)


def main() -> int:
    redactor = configure_logging()
    log.info("KITT starting...")

    load_dotenv()
    try:
        config = load_config()
    except ConfigError as exc:
        log.error("%s", exc)
        return 1
    redactor.add_secret(config.token)

    missing = missing_voice_dependencies()
    if missing:
        log.error(
            "Missing voice dependencies: %s. Run: pip install -r requirements.txt "
            "(discord.py[voice]>=2.7.1 is required for Discord's DAVE voice encryption).",
            ", ".join(missing),
        )
        return 1

    try:
        asyncio.run(run_bot(config))
    except discord.LoginFailure:
        log.error("Login failed: invalid DISCORD_TOKEN.")
        return 1
    except discord.PrivilegedIntentsRequired as exc:
        log.error("Privileged intents required but not enabled: %s", exc)
        return 1
    except KeyboardInterrupt:
        log.info("Interrupted; exiting.")
    except Exception:
        log.exception("KITT stopped because of an unrecoverable error")
        return 1
    log.info("KITT stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
