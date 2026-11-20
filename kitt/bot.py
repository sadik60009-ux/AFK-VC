"""The KITT Discord client."""

from __future__ import annotations

import logging
import time

import discord

from .commands import KittTree, register_commands
from .config import Config
from .voice import VoiceManager

log = logging.getLogger("kitt.bot")


class KittBot(discord.Client):
    """Minimal client that stays in one voice channel."""

    def __init__(self, config: Config) -> None:
        # Only what is needed, and neither intent is privileged:
        #   guilds       - see the guild/channel at all
        #   voice_states - know our own voice state and detect drops/moves
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents)

        self.config = config
        self.started_at = time.monotonic()
        self.tree = KittTree(self)
        self.voice_manager = VoiceManager(self, config.guild_id, config.voice_channel_id)
        self._owner_ids: frozenset[int] | None = None
        self._announced_ready = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def setup_hook(self) -> None:
        """Runs once per process, after login and before the gateway connects."""
        register_commands(self)
        guild = discord.Object(id=self.config.guild_id)
        try:
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d slash command(s).", len(synced))
        except discord.Forbidden:
            log.error(
                "Could not sync slash commands (Missing Access). Re-invite the bot using the "
                "'applications.commands' scope. Voice presence is unaffected."
            )
        except discord.HTTPException as exc:
            log.error("Slash command sync failed: %s. Voice presence is unaffected.", exc)

        await self._load_owner_ids()
        self.voice_manager.start()

    async def close(self) -> None:
        # Stop the supervisor and leave voice *before* the gateway closes.
        await self.voice_manager.stop()
        await super().close()

    # ------------------------------------------------------------------
    # Owner detection
    # ------------------------------------------------------------------

    async def _load_owner_ids(self) -> bool:
        try:
            info = await self.application_info()
        except Exception:
            log.exception("Could not determine the bot owner; owner-only access is unavailable for now")
            return False
        ids = {info.owner.id}
        if info.team is not None:
            ids.update(member.id for member in info.team.members)
        self._owner_ids = frozenset(ids)
        return True

    async def is_owner(self, user: discord.abc.User) -> bool:
        """True if ``user`` owns the application (or is on its team)."""
        if self._owner_ids is None and not await self._load_owner_ids():
            return False
        assert self._owner_ids is not None
        return user.id in self._owner_ids

    # ------------------------------------------------------------------
    # Gateway events
    # ------------------------------------------------------------------

    async def on_ready(self) -> None:
        # on_ready fires again after every full gateway re-identify, so it
        # only *requests a check*; it never runs a retry loop itself.
        if not self._announced_ready:
            self._announced_ready = True
            log.info("Logged in as %s", self.user)
            log.info("Connected to server.")
            log.info("KITT is ready.")
        else:
            log.info("Gateway session re-established.")
        self.voice_manager.request_check("gateway-ready")

    async def on_resumed(self) -> None:
        log.info("Gateway session resumed.")
        self.voice_manager.request_check("gateway-resumed")

    async def on_disconnect(self) -> None:
        log.warning("Gateway connection lost; discord.py will reconnect automatically.")

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if self.user is None or member.id != self.user.id:
            return
        if member.guild.id != self.config.guild_id or self.voice_manager.stopping:
            return
        if before.channel == after.channel:
            return  # mute/deaf/etc. change, not a move or drop
        if after.channel is None:
            log.warning("Disconnected from Voice Channel unexpectedly. Reconnecting...")
            self.voice_manager.request_check("voice-drop")
        elif after.channel.id != self.config.voice_channel_id:
            log.warning("Moved to the wrong Voice Channel ('%s'). Correcting...", after.channel.name)
            self.voice_manager.request_check("voice-moved")
