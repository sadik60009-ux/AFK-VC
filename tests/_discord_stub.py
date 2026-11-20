"""Minimal stand-in for discord.py, installed ONLY if the real library is missing.

It lets the logic in kitt/ (state machine, backoff, command bodies, error
mapping) run in environments without network access. It proves nothing about
discord.py's real API behaviour; see README "Testing" for what that implies.
"""

from __future__ import annotations

import sys
import types

STUBBED = False


def _install() -> None:
    d = types.ModuleType("discord")

    class DiscordException(Exception): ...
    class ClientException(DiscordException): ...
    class LoginFailure(ClientException): ...
    class PrivilegedIntentsRequired(ClientException): ...

    class HTTPException(DiscordException):
        def __init__(self, message: str = "", code: int = 0) -> None:
            super().__init__(message)
            self.code = code

    class Forbidden(HTTPException): ...
    class NotFound(HTTPException): ...

    class Object:
        def __init__(self, id: int) -> None:
            self.id = id

    class Intents:
        guilds = False
        voice_states = False

        @classmethod
        def none(cls) -> "Intents":
            return cls()

    class Client:
        def __init__(self, *, intents: Intents) -> None:
            self.intents = intents

    class VoiceChannel:
        id = 0
        name = ""
        async def connect(self, **kwargs): ...
        def permissions_for(self, member): ...

    class VoiceProtocol: ...
    class Guild: ...
    class Interaction: ...
    class Member: ...
    class VoiceState: ...

    d.DiscordException, d.ClientException = DiscordException, ClientException
    d.LoginFailure, d.PrivilegedIntentsRequired = LoginFailure, PrivilegedIntentsRequired
    d.HTTPException, d.Forbidden, d.NotFound = HTTPException, Forbidden, NotFound
    d.Object, d.Intents, d.Client = Object, Intents, Client
    d.VoiceChannel, d.VoiceProtocol, d.Guild = VoiceChannel, VoiceProtocol, Guild
    d.Interaction, d.Member, d.VoiceState = Interaction, Member, VoiceState
    d.abc = types.SimpleNamespace(User=object)

    ac = types.ModuleType("discord.app_commands")

    class AppCommandError(DiscordException): ...
    class CheckFailure(AppCommandError): ...
    class CommandNotFound(AppCommandError): ...

    class CommandInvokeError(AppCommandError):
        def __init__(self, command, original) -> None:
            super().__init__(str(original))
            self.command, self.original = command, original

    class Command:
        def __init__(self, name, description, callback) -> None:
            self.name, self.description, self.callback = name, description, callback
            self.checks = list(getattr(callback, "__checks__", []))

    def check(predicate):
        def deco(fn):
            fn.__checks__ = [*getattr(fn, "__checks__", []), predicate]
            return fn
        return deco

    class CommandTree:
        def __init__(self, client) -> None:
            self.client = client
            self.commands: dict[str, Command] = {}

        def command(self, *, name, description, guild=None):
            def deco(fn):
                cmd = Command(name, description, fn)
                self.commands[name] = cmd
                return cmd
            return deco

        def get_commands(self, *, guild=None):
            return list(self.commands.values())

        async def sync(self, *, guild=None):
            return list(self.commands.values())

    ac.AppCommandError, ac.CheckFailure = AppCommandError, CheckFailure
    ac.CommandNotFound, ac.CommandInvokeError = CommandNotFound, CommandInvokeError
    ac.Command, ac.check, ac.CommandTree = Command, check, CommandTree
    d.app_commands = ac

    sys.modules["discord"] = d
    sys.modules["discord.app_commands"] = ac


try:
    import discord  # noqa: F401
except ImportError:
    _install()
    STUBBED = True
