"""
KITT - Minimal Discord voice-presence bot.

Purpose: stay connected to a single configured voice channel, 24/7,
silently, on a single server. No audio, no extra features.
"""

import asyncio
import logging
import os
import sys

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID_RAW = os.getenv("GUILD_ID")
VOICE_CHANNEL_ID_RAW = os.getenv("VOICE_CHANNEL_ID")

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("kitt")


def _require_env(name: str, value: str | None) -> str:
    if not value:
        log.error("Missing required environment variable: %s", name)
        sys.exit(1)
    return value


def _require_int_env(name: str, value: str | None) -> int:
    raw = _require_env(name, value)
    try:
        return int(raw)
    except ValueError:
        log.error("Environment variable %s must be an integer, got: %r", name, raw)
        sys.exit(1)


DISCORD_TOKEN = _require_env("DISCORD_TOKEN", DISCORD_TOKEN)
GUILD_ID = _require_int_env("GUILD_ID", GUILD_ID_RAW)
VOICE_CHANNEL_ID = _require_int_env("VOICE_CHANNEL_ID", VOICE_CHANNEL_ID_RAW)

# --------------------------------------------------------------------------
# Reconnect tuning
# --------------------------------------------------------------------------

MIN_RETRY_DELAY = 5       # seconds, first retry
MAX_RETRY_DELAY = 300     # seconds, cap (5 minutes)
WATCHDOG_INTERVAL = 30    # seconds, periodic connection check

# --------------------------------------------------------------------------
# Intents - only what's actually needed.
# guilds: required to see the guild/channel at all.
# voice_states: required to know our own voice connection state and to
# receive voice state update events (needed to detect unexpected drops).
# Neither of these is a privileged intent, so no special toggles are
# needed in the Developer Portal beyond the defaults.
# --------------------------------------------------------------------------

intents = discord.Intents.none()
intents.guilds = True
intents.voice_states = True


class KittBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self._voice_lock = asyncio.Lock()
        self._retry_delay = MIN_RETRY_DELAY
        self._shutting_down = False

    async def setup_hook(self) -> None:
        # Register the /reconnect command to the specific guild only,
        # since this bot only ever operates on one server.
        guild_obj = discord.Object(id=GUILD_ID)
        self.tree.add_command(reconnect_command, guild=guild_obj)
        await self.tree.sync(guild=guild_obj)
        self.watchdog.start()

    async def join_voice_channel(self, *, reason: str = "startup") -> bool:
        """Attempt a single connect to the configured voice channel.

        Returns True on success, False on failure. Never raises.
        """
        async with self._voice_lock:
            guild = self.get_guild(GUILD_ID)
            if guild is None:
                log.error("Configured GUILD_ID %s not found. Is the bot invited to that server?", GUILD_ID)
                return False

            channel = guild.get_channel(VOICE_CHANNEL_ID)
            if channel is None:
                log.error("Configured VOICE_CHANNEL_ID %s not found in guild %s.", VOICE_CHANNEL_ID, guild.name)
                return False

            if not isinstance(channel, discord.VoiceChannel):
                log.error("Configured VOICE_CHANNEL_ID %s is not a voice channel.", VOICE_CHANNEL_ID)
                return False

            existing = guild.voice_client
            if existing is not None and existing.is_connected():
                if existing.channel.id == VOICE_CHANNEL_ID:
                    # Already where we want to be.
                    return True
                # Connected to the wrong channel somehow; move.
                try:
                    await existing.move_to(channel)
                    log.info("Moved to Voice Channel.")
                    return True
                except discord.DiscordException as exc:
                    log.error("Failed to move to configured Voice Channel: %s", exc)
                    return False

            perms = channel.permissions_for(guild.me)
            if not perms.view_channel or not perms.connect:
                log.error(
                    "Missing permission to join Voice Channel '%s'. "
                    "Required: View Channel, Connect.",
                    channel.name,
                )
                return False

            try:
                await channel.connect(reconnect=True, self_deaf=True, self_mute=True)
                log.info("Joined Voice Channel. (%s)", reason)
                return True
            except discord.ClientException as exc:
                # Usually "already connected" races; treat as non-fatal.
                log.warning("Voice connect issue (%s): %s", reason, exc)
                return False
            except (asyncio.TimeoutError, discord.DiscordException) as exc:
                log.error("Failed to join Voice Channel (%s): %s", reason, exc)
                return False

    async def ensure_connected_with_backoff(self, *, reason: str) -> None:
        """Keep retrying the voice connection with increasing backoff
        until it succeeds or the bot is shutting down."""
        while not self._shutting_down:
            ok = await self.join_voice_channel(reason=reason)
            if ok:
                self._retry_delay = MIN_RETRY_DELAY
                return
            log.info("Retrying voice connection in %s seconds...", self._retry_delay)
            await asyncio.sleep(self._retry_delay)
            self._retry_delay = min(self._retry_delay * 2, MAX_RETRY_DELAY)

    async def on_ready(self) -> None:
        log.info("Logged in as %s", self.user)
        log.info("Connected to server.")
        await self.ensure_connected_with_backoff(reason="startup")
        log.info("KITT is ready.")

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        # Only care about our own voice state.
        if member.id != self.user.id:
            return

        # We were connected and now we're not -> unexpected disconnect.
        if before.channel is not None and after.channel is None and not self._shutting_down:
            log.warning("Disconnected from Voice Channel unexpectedly. Reconnecting...")
            asyncio.create_task(self.ensure_connected_with_backoff(reason="voice-drop"))

    @tasks.loop(seconds=WATCHDOG_INTERVAL)
    async def watchdog(self) -> None:
        """Periodic safety net in case an event is missed."""
        if self._shutting_down or not self.is_ready():
            return
        guild = self.get_guild(GUILD_ID)
        if guild is None:
            return
        vc = guild.voice_client
        if vc is None or not vc.is_connected():
            log.warning("Watchdog: not connected to Voice Channel. Reconnecting...")
            await self.ensure_connected_with_backoff(reason="watchdog")

    @watchdog.before_loop
    async def before_watchdog(self) -> None:
        await self.wait_until_ready()

    async def close(self) -> None:
        self._shutting_down = True
        guild = self.get_guild(GUILD_ID)
        if guild is not None and guild.voice_client is not None:
            try:
                await guild.voice_client.disconnect(force=True)
            except discord.DiscordException:
                pass
        await super().close()


client = KittBot()


def _is_owner_or_admin(interaction: discord.Interaction) -> bool:
    if interaction.user.id == interaction.client.application_id:
        return True
    member = interaction.user
    if isinstance(member, discord.Member) and member.guild_permissions.administrator:
        return True
    return False


@app_commands.command(name="reconnect", description="Manually reconnect the bot to the configured voice channel.")
async def reconnect_command(interaction: discord.Interaction) -> None:
    is_admin = isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator
    is_owner = await client.is_owner(interaction.user)

    if not (is_admin or is_owner):
        await interaction.response.send_message(
            "You must be a server administrator or the bot owner to use this command.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    ok = await client.join_voice_channel(reason="manual /reconnect")
    if ok:
        await interaction.followup.send("Reconnected to the configured Voice Channel.", ephemeral=True)
    else:
        await interaction.followup.send(
            "Failed to reconnect. Check the logs for details (missing channel or permissions).",
            ephemeral=True,
        )


def main() -> None:
    log.info("KITT starting...")
    try:
        client.run(DISCORD_TOKEN, log_handler=None)
    except discord.LoginFailure:
        log.error("Login failed: invalid DISCORD_TOKEN.")
        sys.exit(1)
    except discord.PrivilegedIntentsRequired as exc:
        log.error("Privileged intents required but not enabled: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
