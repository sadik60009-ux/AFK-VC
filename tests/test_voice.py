import asyncio
import random
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

from tests import _discord_stub  # noqa: F401
import discord

from kitt import voice
from kitt.voice import MAX_RETRY_DELAY, MIN_RETRY_DELAY, Outcome, VoiceManager, compute_backoff

GUILD_ID, CHANNEL_ID = 1, 2


class FakeVC:
    def __init__(self, channel, connected=True):
        self.channel = channel
        self._connected = connected
        self.guild = None
        self.disconnect = AsyncMock(side_effect=self._disconnect)
        self.move_to = AsyncMock(side_effect=self._move_to)

    def is_connected(self):
        return self._connected

    async def _disconnect(self, *, force=False):
        self._connected = False
        if self.guild is not None and self.guild.voice_client is self:
            self.guild.voice_client = None

    async def _move_to(self, channel):
        self.channel = channel


class FakeGuild:
    def __init__(self, channel):
        self.name = "TestGuild"
        self.me = object()
        self.voice_client = None
        self.channel = channel

    def get_channel(self, channel_id):
        return self.channel if self.channel is not None and channel_id == CHANNEL_ID else None


class FakeClient:
    def __init__(self, guild):
        self.guild = guild
        self.ready = True

    def is_ready(self):
        return self.ready

    def get_guild(self, guild_id):
        return self.guild if guild_id == GUILD_ID else None

    async def wait_until_ready(self):
        return


def make_channel():
    ch = MagicMock(spec=discord.VoiceChannel)
    ch.id, ch.name = CHANNEL_ID, "General"
    ch.permissions_for.return_value = SimpleNamespace(view_channel=True, connect=True)
    return ch


class VoiceTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.channel = make_channel()
        self.guild = FakeGuild(self.channel)
        self.client = FakeClient(self.guild)
        self.connect_delay = 0.0
        self.channel.connect = AsyncMock(side_effect=self._connect_ok)
        self.mgr = VoiceManager(self.client, GUILD_ID, CHANNEL_ID)

    async def asyncTearDown(self):
        await self.mgr.stop()

    async def _connect_ok(self, **kwargs):
        if self.connect_delay:
            await asyncio.sleep(self.connect_delay)
        vc = FakeVC(self.channel)
        vc.guild = self.guild
        self.guild.voice_client = vc
        return vc

    def fail_connect(self, exc):
        self.channel.connect.side_effect = AsyncMock(side_effect=exc)

    def seconds_until_retry(self):
        return self.mgr._next_attempt_at - time.monotonic()


class BackoffTests(unittest.TestCase):
    def test_zero_failures(self):
        self.assertEqual(compute_backoff(0), 0.0)

    def test_grows_and_is_capped(self):
        rng = random.Random(1)
        delays = [compute_backoff(n, rng) for n in range(1, 40)]
        self.assertTrue(all(0 < d <= MAX_RETRY_DELAY for d in delays))
        self.assertLess(delays[0], MIN_RETRY_DELAY * 1.21)
        self.assertGreater(delays[2], delays[0])
        self.assertGreater(delays[-1], MAX_RETRY_DELAY * 0.79)  # saturates near the cap

    def test_huge_failure_count_does_not_overflow(self):
        self.assertLessEqual(compute_backoff(10**9), MAX_RETRY_DELAY)

    def test_jitter_varies(self):
        self.assertGreater(len({compute_backoff(3, random.Random(i)) for i in range(20)}), 1)


class HealTests(VoiceTestCase):
    async def test_connects_when_not_connected(self):
        self.assertEqual(await self.mgr._heal("t", force=False), Outcome.CONNECTED)
        self.channel.connect.assert_awaited_once()
        kwargs = self.channel.connect.await_args.kwargs
        self.assertTrue(kwargs["reconnect"] and kwargs["self_deaf"])
        self.assertTrue(self.mgr.status().healthy)

    async def test_healthy_does_nothing(self):
        await self.mgr._heal("t", force=False)
        self.assertEqual(await self.mgr._heal("t", force=False), Outcome.ALREADY_CONNECTED)
        self.assertEqual(self.channel.connect.await_count, 1)

    async def test_concurrent_heals_produce_one_connection(self):
        self.connect_delay = 0.05
        results = await asyncio.gather(*[self.mgr._heal("t", force=False) for _ in range(10)])
        self.assertEqual(self.channel.connect.await_count, 1)
        self.assertEqual(results.count(Outcome.CONNECTED), 1)
        self.assertEqual(results.count(Outcome.ALREADY_CONNECTED), 9)

    async def test_failure_sets_backoff_and_blocks_immediate_retry(self):
        self.fail_connect(discord.ClientException("boom"))
        self.assertEqual(await self.mgr._heal("t", force=False), Outcome.FAILED)
        self.assertEqual(self.mgr._failures, 1)
        self.assertTrue(MIN_RETRY_DELAY * 0.7 < self.seconds_until_retry() <= MIN_RETRY_DELAY * 1.25)
        self.assertEqual(await self.mgr._heal("t", force=False), Outcome.WAITING)
        self.assertEqual(self.channel.connect.await_count, 1)

    async def test_backoff_grows_then_resets_on_success(self):
        self.fail_connect(discord.ClientException("boom"))
        await self.mgr._heal("t", force=False)
        self.mgr._next_attempt_at = 0
        await self.mgr._heal("t", force=False)
        self.assertEqual(self.mgr._failures, 2)
        self.assertTrue(MIN_RETRY_DELAY * 2 * 0.7 < self.seconds_until_retry() <= MIN_RETRY_DELAY * 2 * 1.25)
        self.channel.connect.side_effect = self._connect_ok
        self.mgr._next_attempt_at = 0
        self.assertEqual(await self.mgr._heal("t", force=False), Outcome.CONNECTED)
        self.assertEqual((self.mgr._failures, self.mgr._last_error, self.mgr._next_attempt_at), (0, None, 0.0))

    async def test_forced_reconnect_bypasses_backoff(self):
        self.fail_connect(discord.ClientException("boom"))
        await self.mgr._heal("t", force=False)
        self.channel.connect.side_effect = self._connect_ok
        self.assertEqual(await self.mgr.reconnect_now(), Outcome.CONNECTED)
        self.assertEqual(self.mgr._failures, 0)

    async def test_reconnect_now_reports_already_connected(self):
        await self.mgr._heal("t", force=False)
        self.assertEqual(await self.mgr.reconnect_now(), Outcome.ALREADY_CONNECTED)
        self.assertEqual(self.channel.connect.await_count, 1)

    async def test_reconnect_now_busy_when_attempt_running(self):
        async with self.mgr._lock:
            self.assertEqual(await self.mgr.reconnect_now(), Outcome.BUSY)

    async def test_concurrent_manual_reconnects_connect_once(self):
        self.connect_delay = 0.05
        results = await asyncio.gather(*[self.mgr.reconnect_now() for _ in range(5)])
        self.assertEqual(self.channel.connect.await_count, 1)
        self.assertEqual(results.count(Outcome.CONNECTED), 1)
        self.assertEqual(results.count(Outcome.BUSY), 4)

    async def test_wrong_channel_is_moved_not_reconnected(self):
        vc = FakeVC(SimpleNamespace(id=999, name="Other"))
        vc.guild = self.guild
        self.guild.voice_client = vc
        self.assertEqual(await self.mgr._heal("t", force=False), Outcome.CONNECTED)
        vc.move_to.assert_awaited_once_with(self.channel)
        vc.disconnect.assert_not_awaited()
        self.channel.connect.assert_not_awaited()

    async def test_stale_client_gets_grace_period_then_is_replaced(self):
        stale = FakeVC(self.channel, connected=False)
        stale.guild = self.guild
        self.guild.voice_client = stale
        self.assertEqual(await self.mgr._heal("t", force=False), Outcome.WAITING)
        stale.disconnect.assert_not_awaited()
        self.channel.connect.assert_not_awaited()

        self.mgr._unhealthy_since -= voice.LIBRARY_RECONNECT_GRACE + 1
        self.assertEqual(await self.mgr._heal("t", force=False), Outcome.CONNECTED)
        stale.disconnect.assert_awaited_once_with(force=True)
        self.channel.connect.assert_awaited_once()

    async def test_library_self_recovery_during_grace_is_noticed(self):
        stale = FakeVC(self.channel, connected=False)
        self.guild.voice_client = stale
        await self.mgr._heal("t", force=False)
        stale._connected = True  # discord.py reconnected by itself
        self.assertEqual(await self.mgr._heal("t", force=False), Outcome.ALREADY_CONNECTED)
        stale.disconnect.assert_not_awaited()
        self.channel.connect.assert_not_awaited()
        self.assertIsNone(self.mgr._unhealthy_since)

    async def test_forced_reconnect_skips_grace(self):
        stale = FakeVC(self.channel, connected=False)
        stale.guild = self.guild
        self.guild.voice_client = stale
        self.assertEqual(await self.mgr.reconnect_now(), Outcome.CONNECTED)
        stale.disconnect.assert_awaited_once()

    async def test_missing_permissions(self):
        self.channel.permissions_for.return_value = SimpleNamespace(view_channel=True, connect=False)
        self.assertEqual(await self.mgr._heal("t", force=False), Outcome.FAILED)
        self.assertIn("Missing permission", self.mgr._last_error)
        self.channel.connect.assert_not_awaited()

    async def test_deleted_channel(self):
        self.guild.channel = None
        self.assertEqual(await self.mgr._heal("t", force=False), Outcome.FAILED)
        self.assertIn("not found", self.mgr._last_error)

    async def test_configured_channel_not_voice(self):
        self.guild.channel = object()
        self.assertEqual(await self.mgr._heal("t", force=False), Outcome.FAILED)
        self.assertIn("not a voice channel", self.mgr._last_error)

    async def test_guild_unavailable(self):
        self.client.guild = None
        self.assertEqual(await self.mgr._heal("t", force=False), Outcome.FAILED)
        self.assertIn("GUILD_ID", self.mgr._last_error)

    async def test_not_ready_waits(self):
        self.client.ready = False
        self.assertEqual(await self.mgr._heal("t", force=False), Outcome.WAITING)
        self.channel.connect.assert_not_awaited()

    async def test_timeout_cleans_up_half_open_client(self):
        half_open = FakeVC(self.channel, connected=False)
        half_open.guild = self.guild

        async def timeout(**kw):
            self.guild.voice_client = half_open
            raise asyncio.TimeoutError()

        self.channel.connect.side_effect = timeout
        self.assertEqual(await self.mgr._heal("t", force=False), Outcome.FAILED)
        half_open.disconnect.assert_awaited_once_with(force=True)
        self.assertIsNone(self.guild.voice_client)
        self.assertIn("Timed out", self.mgr._last_error)

    async def test_each_error_family_is_handled(self):
        errors = [
            discord.Forbidden("no"),
            discord.ClientException("already connected"),
            discord.HTTPException("api"),
            OSError("network down"),
            RuntimeError("weird"),
        ]
        for exc in errors:
            with self.subTest(exc=type(exc).__name__):
                self.fail_connect(exc)
                self.mgr._next_attempt_at = 0
                self.assertEqual(await self.mgr._heal("t", force=False), Outcome.FAILED)
                self.assertTrue(self.mgr._last_error)

    async def test_stopping_refuses_work(self):
        await self.mgr.stop()
        self.assertEqual(await self.mgr._heal("t", force=False), Outcome.STOPPING)
        self.channel.connect.assert_not_awaited()

    async def test_status_summaries(self):
        self.assertIn("recovery pending", self.mgr.status().summary)
        self.fail_connect(discord.ClientException("boom"))
        await self.mgr._heal("t", force=False)
        st = self.mgr.status()
        self.assertIn("Retrying", st.summary)
        self.assertEqual(st.failures, 1)
        self.assertEqual(st.last_error, "Voice client error: boom")
        self.channel.connect.side_effect = self._connect_ok
        await self.mgr.reconnect_now()
        st = self.mgr.status()
        self.assertTrue(st.healthy)
        self.assertEqual((st.channel_name, st.last_error), ("General", None))


class SupervisorTests(VoiceTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(voice, "WATCHDOG_INTERVAL", 0.05)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_start_is_idempotent_single_task(self):
        self.mgr.start()
        first = self.mgr._task
        self.mgr.start()
        self.assertIs(self.mgr._task, first)
        names = [t.get_name() for t in asyncio.all_tasks()]
        self.assertEqual(names.count("kitt-voice-supervisor"), 1)

    async def test_connects_on_startup_and_recovers_on_drop_event(self):
        self.mgr.start()
        await asyncio.sleep(0.1)
        self.assertEqual(self.channel.connect.await_count, 1)

        self.guild.voice_client = None  # kicked
        self.mgr.request_check("voice-drop")
        await asyncio.sleep(0.1)
        self.assertEqual(self.channel.connect.await_count, 2)
        self.assertTrue(self.mgr.status().healthy)

    async def test_watchdog_recovers_without_any_event(self):
        self.mgr.start()
        await asyncio.sleep(0.1)
        self.guild.voice_client = None
        await asyncio.sleep(0.2)
        self.assertEqual(self.channel.connect.await_count, 2)

    async def test_event_storm_during_outage_causes_one_attempt(self):
        self.fail_connect(discord.ClientException("down"))
        self.mgr.start()
        for _ in range(50):
            self.mgr.request_check("voice-drop")
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.2)  # several watchdog cycles too
        self.assertEqual(self.channel.connect.await_count, 1)

    async def test_stop_cancels_task_and_leaves_voice(self):
        self.mgr.start()
        await asyncio.sleep(0.1)
        vc = self.guild.voice_client
        task = self.mgr._task
        await self.mgr.stop()
        await self.mgr.stop()  # idempotent
        self.assertTrue(task.done())
        vc.disconnect.assert_awaited_once_with(force=True)
        self.mgr._wake.clear()
        self.mgr.request_check("late")
        self.assertFalse(self.mgr._wake.is_set())  # ignored once stopping
        self.assertNotIn("kitt-voice-supervisor", [t.get_name() for t in asyncio.all_tasks() if not t.done()])

    async def test_stop_during_connect_attempt(self):
        self.connect_delay = 5
        self.mgr.start()
        await asyncio.sleep(0.1)  # supervisor is now inside connect()
        await asyncio.wait_for(self.mgr.stop(), timeout=2)
        self.assertFalse(self.mgr._lock.locked())

    async def test_unexpected_supervisor_error_does_not_kill_it(self):
        calls = []
        real_heal = self.mgr._heal

        async def flaky(reason, *, force):
            calls.append(reason)
            if len(calls) == 1:
                raise ValueError("bug")
            return await real_heal(reason, force=force)

        self.mgr._heal = flaky
        with mock.patch.object(voice, "MIN_RETRY_DELAY", 0.05):
            self.mgr.start()
            await asyncio.sleep(0.4)
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(self.channel.connect.await_count, 1)


if __name__ == "__main__":
    unittest.main()
