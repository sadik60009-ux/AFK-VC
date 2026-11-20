"""Tests for commands, bot event logic and main().

The command/bot tests drive our own code through the minimal discord stand-in,
so they only run when the real discord.py is NOT installed (the stub's API is
shaped after discord.py but is not discord.py).
"""

import asyncio
import io
import logging
import math
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

from tests import _discord_stub
import discord

from kitt.config import Config
from kitt.voice import Outcome, VoiceStatus

STUB_ONLY = unittest.skipUnless(_discord_stub.STUBBED, "needs the discord stand-in")
CFG = Config(token="tok.en.secret", guild_id=1, voice_channel_id=2)


def status(**kw):
    base = dict(connected=True, healthy=True, channel_name="General", summary="Healthy - no reconnect needed",
                failures=0, retry_in=None, last_error=None)
    base.update(kw)
    return VoiceStatus(**base)


class FakeResponse:
    def __init__(self):
        self.done, self.sent, self.deferred = False, [], False

    def is_done(self):
        return self.done

    async def send_message(self, content, *, ephemeral=False):
        self.done = True
        self.sent.append((content, ephemeral))

    async def defer(self, *, ephemeral=False, thinking=False):
        self.done, self.deferred = True, True


def make_interaction(admin=False, command="x"):
    followup = SimpleNamespace(sent=[])

    async def fsend(content, *, ephemeral=False):
        followup.sent.append((content, ephemeral))

    followup.send = fsend
    return SimpleNamespace(
        response=FakeResponse(), followup=followup, edit_original_response=AsyncMock(),
        permissions=SimpleNamespace(administrator=admin), user=SimpleNamespace(id=7),
        command=SimpleNamespace(name=command),
    )


def make_bot():
    from kitt.commands import KittTree, register_commands

    bot = SimpleNamespace(config=CFG, started_at=0.0, latency=0.0421, is_owner=AsyncMock(return_value=False))
    bot.is_ready = lambda: True
    bot.tree = KittTree(bot)
    bot.voice_manager = MagicMock()
    bot.voice_manager.status.return_value = status()
    bot.voice_manager.reconnect_now = AsyncMock(return_value=Outcome.CONNECTED)
    register_commands(bot)
    return bot


class HelperTests(unittest.TestCase):
    def test_format_duration(self):
        from kitt.commands import format_duration
        self.assertEqual(format_duration(5), "5s")
        self.assertEqual(format_duration(65), "1m 5s")
        self.assertEqual(format_duration(3600), "1h 0m 0s")
        self.assertEqual(format_duration(2 * 86400 + 3 * 3600 + 4 * 60 + 5), "2d 3h 4m 5s")
        self.assertEqual(format_duration(-3), "0s")

    def test_format_latency(self):
        from kitt.commands import format_latency
        self.assertEqual(format_latency(0.0421), "42 ms")
        self.assertEqual(format_latency(math.nan), "unavailable")
        self.assertEqual(format_latency(math.inf), "unavailable")


@STUB_ONLY
class CommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = make_bot()
        self.cmds = self.bot.tree.commands

    async def test_all_four_commands_registered(self):
        self.assertEqual(set(self.cmds), {"status", "reconnect", "ping", "help"})
        for cmd in self.cmds.values():
            self.assertTrue(cmd.description and len(cmd.description) <= 100)

    async def test_only_reconnect_has_a_check(self):
        self.assertEqual({n for n, c in self.cmds.items() if c.checks}, {"reconnect"})

    async def test_status_text(self):
        i = make_interaction()
        await self.cmds["status"].callback(i)
        text, ephemeral = i.response.sent[0]
        self.assertTrue(ephemeral)
        for needle in ("Online: Yes", "Connected to **General**", "Uptime:", "Latency: 42 ms", "Reconnect: Healthy"):
            self.assertIn(needle, text)

    async def test_status_when_disconnected_shows_error(self):
        self.bot.voice_manager.status.return_value = status(
            connected=False, healthy=False, channel_name=None, summary="Retrying", failures=2,
            retry_in=30.0, last_error="Missing permission")
        i = make_interaction()
        await self.cmds["status"].callback(i)
        text = i.response.sent[0][0]
        self.assertIn("Not connected", text)
        self.assertIn("Last error: Missing permission", text)

    async def test_status_nan_latency(self):
        self.bot.latency = math.nan
        i = make_interaction()
        await self.cmds["status"].callback(i)
        self.assertIn("Latency: unavailable", i.response.sent[0][0])

    async def test_ping(self):
        i = make_interaction()
        await self.cmds["ping"].callback(i)
        i.edit_original_response.assert_awaited_once()
        content = i.edit_original_response.await_args.kwargs["content"]
        self.assertIn("Gateway latency: 42 ms", content)
        self.assertIn("API response time:", content)

    async def test_help_lists_everything_and_marks_restricted(self):
        i = make_interaction()
        await self.cmds["help"].callback(i)
        text = i.response.sent[0][0]
        for name in ("status", "reconnect", "ping", "help"):
            self.assertIn(f"`/{name}`", text)
        reconnect_line = next(l for l in text.splitlines() if "/reconnect" in l)
        self.assertIn("administrators and the bot owner", reconnect_line)
        status_line = next(l for l in text.splitlines() if "/status" in l)
        self.assertIn("everyone", status_line)

    async def test_reconnect_check_allows_admin_owner_and_denies_others(self):
        check = self.cmds["reconnect"].checks[0]
        self.assertTrue(await check(make_interaction(admin=True)))
        self.bot.is_owner.return_value = True
        self.assertTrue(await check(make_interaction(admin=False)))
        self.bot.is_owner.return_value = False
        self.assertFalse(await check(make_interaction(admin=False)))

    async def test_reconnect_outcomes(self):
        expectations = {
            Outcome.CONNECTED: "Reconnected",
            Outcome.ALREADY_CONNECTED: "Already connected to **General**",
            Outcome.BUSY: "already in progress",
            Outcome.STOPPING: "shutting down",
            Outcome.WAITING: "not connected to the Discord gateway",
        }
        for outcome, needle in expectations.items():
            with self.subTest(outcome=outcome):
                self.bot.voice_manager.reconnect_now.return_value = outcome
                i = make_interaction(admin=True)
                await self.cmds["reconnect"].callback(i)
                self.assertTrue(i.response.deferred)
                content, ephemeral = i.followup.sent[0]
                self.assertTrue(ephemeral)
                self.assertIn(needle, content)

    async def test_reconnect_failure_message(self):
        self.bot.voice_manager.reconnect_now.return_value = Outcome.FAILED
        self.bot.voice_manager.status.return_value = status(
            connected=False, healthy=False, last_error="Missing permission", retry_in=12.0, failures=1)
        i = make_interaction(admin=True)
        await self.cmds["reconnect"].callback(i)
        content = i.followup.sent[0][0]
        self.assertIn("Failed to reconnect: Missing permission", content)
        self.assertIn("next try in 12s", content)


@STUB_ONLY
class ErrorHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from kitt.commands import KittTree
        self.tree = KittTree(SimpleNamespace())
        self.ac = sys.modules["discord.app_commands"]

    async def handle(self, error, interaction=None):
        i = interaction or make_interaction()
        await self.tree.on_error(i, error)
        return i

    def reply(self, i):
        return (i.response.sent or i.followup.sent)[0][0]

    async def test_check_failure(self):
        i = await self.handle(self.ac.CheckFailure("nope"))
        self.assertIn("administrators and the bot owner", self.reply(i))

    async def test_invoke_error_wrapping_unexpected_exception_is_logged_and_generic(self):
        err = self.ac.CommandInvokeError(None, ValueError("secret detail"))
        with self.assertLogs("kitt.commands", "ERROR"):
            i = await self.handle(err)
        self.assertIn("Something went wrong", self.reply(i))
        self.assertNotIn("secret detail", self.reply(i))

    async def test_forbidden(self):
        with self.assertLogs("kitt.commands", "ERROR"):
            i = await self.handle(self.ac.CommandInvokeError(None, discord.Forbidden("x")))
        self.assertIn("permission", self.reply(i))

    async def test_http_exception(self):
        with self.assertLogs("kitt.commands", "ERROR"):
            i = await self.handle(self.ac.CommandInvokeError(None, discord.HTTPException("x")))
        self.assertIn("try again", self.reply(i))

    async def test_expired_interaction_is_logged_and_nothing_sent(self):
        with self.assertLogs("kitt.commands", "WARNING"):
            i = await self.handle(self.ac.CommandInvokeError(None, discord.NotFound("unknown", code=10062)))
        self.assertEqual((i.response.sent, i.followup.sent), ([], []))

    async def test_reply_after_defer_uses_followup(self):
        i = make_interaction()
        i.response.done = True
        await self.handle(self.ac.CheckFailure("x"), i)
        self.assertEqual(len(i.followup.sent), 1)

    async def test_send_failure_while_reporting_does_not_raise(self):
        i = make_interaction()
        i.response.send_message = AsyncMock(side_effect=discord.NotFound("gone", code=10062))
        with self.assertLogs("kitt.commands", "WARNING"):
            await self.handle(self.ac.CheckFailure("x"), i)


@STUB_ONLY
class BotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from kitt.bot import KittBot
        self.bot = KittBot(CFG)
        self.bot.user = SimpleNamespace(id=100)
        self.bot.voice_manager = MagicMock(stopping=False)

    def test_intents_minimal(self):
        self.assertTrue(self.bot.intents.guilds and self.bot.intents.voice_states)

    async def test_owner_detection_and_caching(self):
        self.bot.application_info = AsyncMock(return_value=SimpleNamespace(owner=SimpleNamespace(id=42), team=None))
        self.assertTrue(await self.bot.is_owner(SimpleNamespace(id=42)))
        self.assertFalse(await self.bot.is_owner(SimpleNamespace(id=43)))
        self.bot.application_info.assert_awaited_once()

    async def test_team_members_count_as_owners(self):
        team = SimpleNamespace(members=[SimpleNamespace(id=5), SimpleNamespace(id=6)])
        self.bot.application_info = AsyncMock(return_value=SimpleNamespace(owner=SimpleNamespace(id=4), team=team))
        for uid in (4, 5, 6):
            self.assertTrue(await self.bot.is_owner(SimpleNamespace(id=uid)))

    async def test_owner_lookup_failure_denies_and_retries_later(self):
        self.bot.application_info = AsyncMock(side_effect=discord.HTTPException("down"))
        with self.assertLogs("kitt.bot", "ERROR"):
            self.assertFalse(await self.bot.is_owner(SimpleNamespace(id=42)))
        self.bot.application_info = AsyncMock(return_value=SimpleNamespace(owner=SimpleNamespace(id=42), team=None))
        self.assertTrue(await self.bot.is_owner(SimpleNamespace(id=42)))

    def vs(self, channel_id):
        return SimpleNamespace(channel=None if channel_id is None else SimpleNamespace(id=channel_id, name=f"c{channel_id}"))

    def member(self, uid=100, guild=1):
        return SimpleNamespace(id=uid, guild=SimpleNamespace(id=guild))

    async def test_voice_state_drop_requests_check(self):
        with self.assertLogs("kitt.bot", "WARNING"):
            await self.bot.on_voice_state_update(self.member(), self.vs(2), self.vs(None))
        self.bot.voice_manager.request_check.assert_called_once_with("voice-drop")

    async def test_voice_state_moved_requests_check(self):
        with self.assertLogs("kitt.bot", "WARNING"):
            await self.bot.on_voice_state_update(self.member(), self.vs(2), self.vs(9))
        self.bot.voice_manager.request_check.assert_called_once_with("voice-moved")

    async def test_voice_state_ignored_cases(self):
        same = self.vs(2)
        cases = [
            (self.member(uid=555), self.vs(2), self.vs(None)),  # someone else
            (self.member(guild=99), self.vs(2), self.vs(None)),  # other guild
            (self.member(), same, same),  # mute/deafen only
            (self.member(), self.vs(None), self.vs(2)),  # our own successful join
        ]
        for m, b, a in cases:
            await self.bot.on_voice_state_update(m, b, a)
        self.bot.voice_manager.request_check.assert_not_called()

    async def test_voice_state_ignored_while_stopping(self):
        self.bot.voice_manager.stopping = True
        await self.bot.on_voice_state_update(self.member(), self.vs(2), self.vs(None))
        self.bot.voice_manager.request_check.assert_not_called()

    async def test_on_ready_only_requests_check_never_loops(self):
        with self.assertLogs("kitt.bot", "INFO") as logs:
            await self.bot.on_ready()
            await self.bot.on_ready()
        self.assertEqual(sum("KITT is ready." in m for m in logs.output), 1)
        self.assertEqual(self.bot.voice_manager.request_check.call_count, 2)

    async def test_on_resumed_requests_check(self):
        with self.assertLogs("kitt.bot", "INFO"):
            await self.bot.on_resumed()
        self.bot.voice_manager.request_check.assert_called_once_with("gateway-resumed")

    async def test_setup_hook_survives_sync_failure_and_starts_voice(self):
        for exc in (discord.Forbidden("Missing Access"), discord.HTTPException("boom")):
            with self.subTest(exc=type(exc).__name__):
                self.bot.voice_manager.start.reset_mock()
                self.bot.tree.sync = AsyncMock(side_effect=exc)
                self.bot.application_info = AsyncMock(return_value=SimpleNamespace(owner=SimpleNamespace(id=1), team=None))
                with self.assertLogs("kitt.bot", "ERROR"):
                    await self.bot.setup_hook()
                self.bot.voice_manager.start.assert_called_once()
                self.assertEqual(set(self.bot.tree.commands), {"status", "reconnect", "ping", "help"})

    async def test_close_stops_voice_before_gateway(self):
        order = []
        self.bot.voice_manager.stop = AsyncMock(side_effect=lambda: order.append("voice"))

        async def base_close(self_):
            order.append("gateway")

        with mock.patch.object(type(self.bot).__mro__[1], "close", base_close, create=True):
            await self.bot.close()
        self.assertEqual(order, ["voice", "gateway"])


class MainTests(unittest.TestCase):
    def test_redacting_filter_hides_token_everywhere(self):
        import main
        redactor = main.RedactingFilter()
        redactor.add_secret("tok.en.secret")
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        handler.addFilter(redactor)
        lg = logging.getLogger("redaction-test")
        lg.propagate, lg.handlers = False, [handler]
        lg.error("bad token %s", "tok.en.secret")
        try:
            raise ValueError("header Bot tok.en.secret rejected")
        except ValueError:
            lg.exception("failed with tok.en.secret")
        out = stream.getvalue()
        self.assertNotIn("tok.en.secret", out)
        self.assertGreaterEqual(out.count("[REDACTED]"), 3)

    def run_main(self, env, **patches):
        import main
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(main, "load_dotenv"), \
             mock.patch.object(main, "configure_logging", lambda: main.RedactingFilter()), \
             mock.patch.object(main, "run_bot", MagicMock(return_value=None)):
            for name, value in patches.items():
                stack = mock.patch.object(main, name, value)
                stack.start()
                self.addCleanup(stack.stop)
            with self.assertLogs(level="INFO") as logs:
                code = main.main()
        return code, "\n".join(logs.output)

    GOOD = {"DISCORD_TOKEN": "tok.en.secret", "GUILD_ID": "1", "VOICE_CHANNEL_ID": "2"}

    def test_invalid_env_exits_1_without_leaking_token(self):
        code, out = self.run_main({"DISCORD_TOKEN": "tok.en.secret", "GUILD_ID": "tok.en.secret"})
        self.assertEqual(code, 1)
        self.assertIn("GUILD_ID", out)
        self.assertIn("VOICE_CHANNEL_ID", out)
        self.assertNotIn("tok.en.secret", out)

    def test_missing_voice_dependency_exits_1_with_hint(self):
        code, out = self.run_main(self.GOOD, missing_voice_dependencies=lambda: ["davey"])
        self.assertEqual(code, 1)
        self.assertIn("davey", out)
        self.assertIn("requirements.txt", out)

    def test_login_failure_exits_1(self):
        import main
        code, out = self.run_main(
            self.GOOD, missing_voice_dependencies=lambda: [],
            asyncio=SimpleNamespace(run=mock.Mock(side_effect=discord.LoginFailure("x"))))
        self.assertEqual(code, 1)
        self.assertIn("invalid DISCORD_TOKEN", out)

    def test_clean_run_exits_0(self):
        code, out = self.run_main(
            self.GOOD, missing_voice_dependencies=lambda: [],
            asyncio=SimpleNamespace(run=mock.Mock(return_value=None)))
        self.assertEqual(code, 0)

    def test_keyboard_interrupt_is_clean(self):
        code, _ = self.run_main(
            self.GOOD, missing_voice_dependencies=lambda: [],
            asyncio=SimpleNamespace(run=mock.Mock(side_effect=KeyboardInterrupt)))
        self.assertEqual(code, 0)

    def test_unexpected_crash_exits_1_and_is_logged(self):
        code, out = self.run_main(
            self.GOOD, missing_voice_dependencies=lambda: [],
            asyncio=SimpleNamespace(run=mock.Mock(side_effect=RuntimeError("kaboom"))))
        self.assertEqual(code, 1)
        self.assertIn("kaboom", out)


if __name__ == "__main__":
    unittest.main()


class SigtermShutdownTest(unittest.TestCase):
    """Real SIGTERM delivered to a real process running main.run_bot()."""

    def test_sigterm_closes_bot_and_exits_cleanly(self):
        import subprocess
        import textwrap

        script = textwrap.dedent(
            """
            import asyncio, os, signal, sys
            from tests import _discord_stub  # noqa
            import main
            from kitt.config import Config

            events = []

            class FakeBot:
                def __init__(self, config):
                    self._closed = asyncio.Event()
                async def __aenter__(self): return self
                async def __aexit__(self, *exc): events.append("aexit")
                async def start(self, token, reconnect=True):
                    events.append("started")
                    asyncio.get_running_loop().call_later(0.2, os.kill, os.getpid(), signal.SIGTERM)
                    await self._closed.wait()
                async def close(self):
                    events.append("close")
                    self._closed.set()

            main.KittBot = FakeBot
            asyncio.run(asyncio.wait_for(main.run_bot(Config("t", 1, 2)), timeout=5))
            print(",".join(events))
            """
        )
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        proc = subprocess.run([sys.executable, "-c", script], cwd=root, capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip().splitlines()[-1], "started,close,aexit")
