"""Slash commands and the global application-command error handler."""

from __future__ import annotations

import logging
import math
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from .voice import Outcome

if TYPE_CHECKING:
    from .bot import KittBot

log = logging.getLogger("kitt.commands")

# Commands that require administrator permission or bot ownership.
RESTRICTED_COMMANDS = frozenset({"reconnect"})

UNKNOWN_INTERACTION = 10062  # Discord JSON error code: interaction expired / unknown


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def format_duration(seconds: float) -> str:
    """Render a duration like ``2d 3h 4m 5s`` (leading zero units omitted)."""
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if days or hours:
        parts.append(f"{hours}h")
    if days or hours or minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def format_latency(latency: float) -> str:
    """discord.py reports NaN/inf while the gateway is not connected."""
    if not math.isfinite(latency):
        return "unavailable"
    return f"{latency * 1000:.0f} ms"


async def send_ephemeral(interaction: discord.Interaction, content: str) -> None:
    """Send an ephemeral reply, tolerating expired interactions and API errors."""
    name = interaction.command.name if interaction.command else "unknown"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)
    except discord.NotFound:
        log.warning("Interaction for /%s expired before a response could be sent.", name)
    except discord.HTTPException as exc:
        log.warning("Could not send response for /%s: %s", name, exc)


# --------------------------------------------------------------------------
# Global error handler
# --------------------------------------------------------------------------


class KittTree(app_commands.CommandTree):
    """Command tree with a single, global error handler."""

    async def on_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        original = error.original if isinstance(error, app_commands.CommandInvokeError) else error
        name = interaction.command.name if interaction.command else "unknown"

        if isinstance(original, app_commands.CheckFailure):
            await send_ephemeral(
                interaction,
                "You don't have permission to use this command. "
                "It is limited to server administrators and the bot owner.",
            )
        elif isinstance(original, app_commands.CommandNotFound):
            await send_ephemeral(interaction, "That command is no longer available.")
        elif isinstance(original, discord.NotFound) and getattr(original, "code", None) == UNKNOWN_INTERACTION:
            # Took longer than 3 seconds to acknowledge; Discord won't accept any reply.
            log.warning("Interaction for /%s expired before it was acknowledged.", name)
        elif isinstance(original, discord.Forbidden):
            log.error("/%s failed: missing Discord permissions: %s", name, original)
            await send_ephemeral(
                interaction, "I'm missing a permission needed for that. Check the bot's role and channel permissions."
            )
        elif isinstance(original, discord.HTTPException):
            log.error("/%s failed with a Discord API error: %s", name, original)
            await send_ephemeral(interaction, "Discord returned an error. Please try again in a moment.")
        else:
            log.error("Unhandled error in /%s", name, exc_info=original)
            await send_ephemeral(interaction, "Something went wrong running that command. The error has been logged.")


# --------------------------------------------------------------------------
# Command registration
# --------------------------------------------------------------------------


def register_commands(bot: KittBot) -> None:
    """Register all slash commands on the bot's tree for the configured guild only."""
    tree = bot.tree
    guild = discord.Object(id=bot.config.guild_id)

    async def is_admin_or_owner(interaction: discord.Interaction) -> bool:
        if interaction.permissions.administrator:
            return True
        return await bot.is_owner(interaction.user)

    @tree.command(name="status", description="Show KITT's connection status.", guild=guild)
    async def status(interaction: discord.Interaction) -> None:
        voice = bot.voice_manager.status()
        online = "Yes" if bot.is_ready() else "No (connecting to Discord)"
        if voice.connected:
            where = f"Connected to **{voice.channel_name}**"
            if not voice.healthy:
                where += " (not the configured channel)"
        else:
            where = "Not connected"
        lines = [
            "**KITT status**",
            f"- Online: {online}",
            f"- Voice: {where}",
            f"- Uptime: {format_duration(time.monotonic() - bot.started_at)}",
            f"- Latency: {format_latency(bot.latency)}",
            f"- Reconnect: {voice.summary}",
        ]
        if voice.last_error:
            lines.append(f"- Last error: {voice.last_error}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @tree.command(name="ping", description="Show Discord gateway latency and response time.", guild=guild)
    async def ping(interaction: discord.Interaction) -> None:
        started = time.monotonic()
        await interaction.response.send_message("Pinging...", ephemeral=True)
        round_trip = (time.monotonic() - started) * 1000
        await interaction.edit_original_response(
            content=(
                "**Pong!**\n"
                f"- Gateway latency: {format_latency(bot.latency)}\n"
                f"- API response time: {round_trip:.0f} ms"
            )
        )

    @tree.command(
        name="reconnect",
        description="Reconnect KITT to the configured voice channel.",
        guild=guild,
    )
    @app_commands.check(is_admin_or_owner)
    async def reconnect(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        outcome = await bot.voice_manager.reconnect_now()
        voice = bot.voice_manager.status()

        if outcome is Outcome.ALREADY_CONNECTED:
            message = f"Already connected to **{voice.channel_name}**. No reconnect was needed."
        elif outcome is Outcome.CONNECTED:
            message = "Reconnected to the configured voice channel."
        elif outcome is Outcome.BUSY:
            message = "A reconnect attempt is already in progress. Check `/status` in a moment."
        elif outcome is Outcome.STOPPING:
            message = "KITT is shutting down."
        elif outcome is Outcome.WAITING:
            message = "KITT is not connected to the Discord gateway yet. Try again shortly."
        else:
            detail = voice.last_error or "see the logs for details"
            retry = f" KITT will keep retrying automatically (next try in {voice.retry_in:.0f}s)." if voice.retry_in is not None else ""
            message = f"Failed to reconnect: {detail}{retry}"
        await interaction.followup.send(message, ephemeral=True)

    @tree.command(name="help", description="List KITT's commands and who can use them.", guild=guild)
    async def help_command(interaction: discord.Interaction) -> None:
        lines = ["**KITT commands**"]
        for command in sorted(tree.get_commands(guild=guild), key=lambda c: c.name):
            who = (
                "server administrators and the bot owner"
                if command.name in RESTRICTED_COMMANDS
                else "everyone"
            )
            lines.append(f"- `/{command.name}` - {command.description} _Allowed: {who}._")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)
