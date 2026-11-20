"""Voice connection manager.

Design goals (in priority order):

* Exactly one background supervisor task decides when to (re)connect.
  Gateway events, the periodic watchdog and the /reconnect command never
  start their own retry loops; they only *wake* the supervisor or ask it to
  run one forced attempt.
* Exactly one connect/move/disconnect operation at a time (``asyncio.Lock``).
* One shared retry state (failure count + next-attempt time), reset only
  after the connection has actually been verified healthy.
* Exponential backoff with jitter, capped at ``MAX_RETRY_DELAY``.
* Give discord.py's own built-in voice reconnect a short grace period before
  discarding its voice client, so the two mechanisms do not fight.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import random
import time
from dataclasses import dataclass

import discord

log = logging.getLogger("kitt.voice")

MIN_RETRY_DELAY = 5.0  # seconds, delay after the first failed attempt
MAX_RETRY_DELAY = 300.0  # seconds, hard cap on any retry delay
WATCHDOG_INTERVAL = 30.0  # seconds, periodic health check
LIBRARY_RECONNECT_GRACE = 20.0  # seconds, let discord.py self-heal first
CONNECT_TIMEOUT = 30.0  # seconds, per connect/move attempt
JITTER_RANGE = (0.8, 1.2)  # multiplicative jitter applied to each delay


def compute_backoff(failures: int, rng: random.Random | None = None) -> float:
    """Delay before the next attempt after ``failures`` consecutive failures."""
    if failures < 1:
        return 0.0
    exponent = min(failures - 1, 16)  # avoid huge intermediate integers
    base = min(MIN_RETRY_DELAY * (2**exponent), MAX_RETRY_DELAY)
    jitter = (rng or random).uniform(*JITTER_RANGE)
    return min(base * jitter, MAX_RETRY_DELAY)


class Outcome(enum.Enum):
    """Result of a heal/reconnect request."""

    ALREADY_CONNECTED = "already_connected"  # healthy, nothing was done
    CONNECTED = "connected"  # an attempt was made and succeeded
    FAILED = "failed"  # an attempt was made and failed
    WAITING = "waiting"  # no attempt made (backoff / grace / not ready)
    BUSY = "busy"  # another attempt is in progress
    STOPPING = "stopping"  # manager is shutting down


@dataclass(frozen=True, slots=True)
class VoiceStatus:
    """Snapshot of the voice connection for /status."""

    connected: bool
    healthy: bool
    channel_name: str | None
    summary: str
    failures: int
    retry_in: float | None
    last_error: str | None


class VoiceManager:
    """Keeps the bot in a single configured voice channel."""

    def __init__(self, client: discord.Client, guild_id: int, channel_id: int) -> None:
        self._client = client
        self._guild_id = guild_id
        self._channel_id = channel_id

        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._wake_reason = "event"
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

        # Shared retry state. Only mutated while holding ``self._lock``.
        self._failures = 0
        self._next_attempt_at = 0.0
        self._unhealthy_since: float | None = None
        self._recheck_at: float | None = None
        self._last_error: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def stopping(self) -> bool:
        return self._stopping

    def start(self) -> None:
        """Start the (single) supervisor task. Safe to call more than once."""
        if self._stopping:
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._supervise(), name="kitt-voice-supervisor")

    async def stop(self) -> None:
        """Cancel the supervisor and leave voice. Idempotent."""
        self._stopping = True
        self._wake.set()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover - defensive
                log.exception("Voice supervisor ended with an error during shutdown")

        guild = self._client.get_guild(self._guild_id)
        vc = guild.voice_client if guild is not None else None
        if vc is not None:
            await self._discard(vc, "shutdown")

    def request_check(self, reason: str) -> None:
        """Ask the supervisor to re-check soon. Never starts a retry loop."""
        if self._stopping:
            return
        self._wake_reason = reason
        self._wake.set()

    async def reconnect_now(self) -> Outcome:
        """Run one forced attempt (used by /reconnect).

        Returns ``BUSY`` instead of queueing if another attempt is running.
        ``locked()`` and the lock acquisition inside ``_heal`` happen without
        an intervening ``await``, so two callers cannot both pass this check.
        """
        if self._lock.locked():
            return Outcome.BUSY
        return await self._heal("manual /reconnect", force=True)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> VoiceStatus:
        guild = self._client.get_guild(self._guild_id)
        vc = guild.voice_client if guild is not None else None
        connected = vc is not None and bool(vc.is_connected())
        channel = getattr(vc, "channel", None) if connected else None
        healthy = self._is_healthy(vc)
        retry_in: float | None = None
        if not healthy and self._failures > 0:
            retry_in = max(0.0, self._next_attempt_at - time.monotonic())

        if self._stopping:
            summary = "Shutting down"
        elif healthy:
            summary = "Healthy - no reconnect needed"
        elif self._lock.locked():
            summary = "Reconnect attempt in progress"
        elif not self._client.is_ready():
            summary = "Waiting for the Discord gateway"
        elif self._failures > 0:
            summary = f"Retrying - {self._failures} failed attempt(s), next retry in {retry_in:.0f}s"
        elif connected:
            summary = "Connected to the wrong channel - correcting"
        else:
            summary = "Not connected - recovery pending"

        return VoiceStatus(
            connected=connected,
            healthy=healthy,
            channel_name=getattr(channel, "name", None),
            summary=summary,
            failures=self._failures,
            retry_in=retry_in,
            last_error=None if healthy else self._last_error,
        )

    # ------------------------------------------------------------------
    # Supervisor
    # ------------------------------------------------------------------

    async def _supervise(self) -> None:
        await self._client.wait_until_ready()
        reason = "startup"
        while not self._stopping:
            self._wake.clear()  # cleared *before* the check: wakeups during it are kept
            try:
                await self._heal(reason, force=False)
            except Exception:
                log.exception("Unexpected error in voice supervisor; checking again shortly")
                self._recheck_at = time.monotonic() + MIN_RETRY_DELAY
            reason = await self._wait_for_next_check()

    async def _wait_for_next_check(self) -> str:
        timeout = WATCHDOG_INTERVAL
        if self._recheck_at is not None:
            timeout = min(timeout, max(0.5, self._recheck_at - time.monotonic()))
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return "watchdog"
        return self._wake_reason

    # ------------------------------------------------------------------
    # Core logic
    # ------------------------------------------------------------------

    def _is_healthy(self, vc: discord.VoiceProtocol | None) -> bool:
        if vc is None or not vc.is_connected():
            return False
        channel = getattr(vc, "channel", None)
        return channel is not None and channel.id == self._channel_id

    async def _heal(self, reason: str, *, force: bool) -> Outcome:
        async with self._lock:
            if self._stopping:
                return Outcome.STOPPING
            if not self._client.is_ready():
                return Outcome.WAITING

            self._recheck_at = None
            now = time.monotonic()
            guild = self._client.get_guild(self._guild_id)
            vc = guild.voice_client if guild is not None else None

            if self._is_healthy(vc):
                if self._unhealthy_since is not None:
                    log.info("Voice connection recovered.")
                self._reset_state()
                return Outcome.ALREADY_CONNECTED

            if self._unhealthy_since is None:
                self._unhealthy_since = now
                # A normal cold start is not a problem worth a warning.
                level = logging.INFO if reason == "startup" else logging.WARNING
                log.log(level, "Not connected to the configured Voice Channel (trigger: %s).", reason)

            if force:
                self._failures = 0
                self._next_attempt_at = 0.0
            else:
                if now < self._next_attempt_at:
                    self._recheck_at = self._next_attempt_at
                    return Outcome.WAITING
                if vc is not None and not vc.is_connected():
                    # discord.py may be reconnecting this client by itself.
                    grace_end = self._unhealthy_since + LIBRARY_RECONNECT_GRACE
                    if now < grace_end:
                        self._recheck_at = grace_end
                        return Outcome.WAITING

            if guild is None:
                error: str | None = (
                    f"Configured GUILD_ID {self._guild_id} is not available. "
                    "Is the bot invited to that server, or is Discord having an outage?"
                )
            else:
                error = await self._connect(guild, vc, reason)

            if error is None:
                self._reset_state()
                return Outcome.CONNECTED
            self._mark_failed(error)
            return Outcome.FAILED

    async def _connect(
        self, guild: discord.Guild, vc: discord.VoiceProtocol | None, reason: str
    ) -> str | None:
        """One connect/move attempt. Returns an error message, or None on success."""
        channel = guild.get_channel(self._channel_id)
        if channel is None:
            return (
                f"Voice channel {self._channel_id} was not found in '{guild.name}' "
                "(deleted, or not visible to the bot)."
            )
        if not isinstance(channel, discord.VoiceChannel):
            return f"Channel {self._channel_id} is not a voice channel."

        me = guild.me
        if me is not None:
            perms = channel.permissions_for(me)
            if not (perms.view_channel and perms.connect):
                return (
                    f"Missing permission to join Voice Channel '{channel.name}'. "
                    "Required: View Channel, Connect."
                )

        error: str
        try:
            if vc is not None and vc.is_connected():
                await vc.move_to(channel)
                log.info("Moved to Voice Channel '%s'. (%s)", channel.name, reason)
            else:
                if vc is not None:
                    await self._discard(vc, "stale voice client")
                await channel.connect(
                    timeout=CONNECT_TIMEOUT, reconnect=True, self_deaf=True, self_mute=True
                )
                log.info("Joined Voice Channel. (%s)", reason)
        except asyncio.TimeoutError:
            error = f"Timed out after {CONNECT_TIMEOUT:.0f}s while connecting to voice."
        except discord.Forbidden:
            error = f"Discord refused the voice connection to '{channel.name}' (Forbidden)."
        except discord.ClientException as exc:
            error = f"Voice client error: {exc}"
        except discord.DiscordException as exc:
            error = f"Discord error while connecting to voice: {exc}"
        except OSError as exc:
            error = f"Network error while connecting to voice: {exc}"
        except Exception as exc:
            log.exception("Unexpected error while connecting to voice")
            error = f"Unexpected {type(exc).__name__} while connecting to voice."
        else:
            return None

        # A failed attempt can leave a half-open client behind, which would
        # make every later connect() fail with "Already connected".
        leftover = guild.voice_client
        if leftover is not None and not leftover.is_connected():
            await self._discard(leftover, "failed attempt")
        return error

    async def _discard(self, vc: discord.VoiceProtocol, why: str) -> None:
        try:
            await vc.disconnect(force=True)
        except Exception as exc:
            log.warning("Error while discarding voice client (%s): %s", why, exc)

    # ------------------------------------------------------------------
    # Retry state
    # ------------------------------------------------------------------

    def _reset_state(self) -> None:
        self._failures = 0
        self._next_attempt_at = 0.0
        self._unhealthy_since = None
        self._recheck_at = None
        self._last_error = None

    def _mark_failed(self, error: str) -> None:
        self._failures += 1
        delay = compute_backoff(self._failures)
        self._next_attempt_at = time.monotonic() + delay
        self._recheck_at = self._next_attempt_at
        self._last_error = error
        log.error("%s", error)
        log.info("Retrying voice connection in %.0f seconds (failed attempts: %d).", delay, self._failures)
