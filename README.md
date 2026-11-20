# KITT

A minimal Discord bot with one job: stay connected to a single voice channel,
silently, and recover by itself from recoverable failures. No audio, no music,
no dashboard, no database.

## Project layout

```
main.py            entrypoint: logging, config, signal handling, startup checks
kitt/config.py     environment loading + validation
kitt/voice.py      voice connection manager (the reliability core)
kitt/commands.py   slash commands + global error handler
kitt/bot.py        discord.Client subclass, gateway/voice event wiring
tests/             unit tests (python -m unittest)
Procfile           worker: python main.py
nixpacks.toml      extra system packages for the Nixpacks builder
.python-version    Python version for Railway (both Nixpacks and Railpack)
```

## 1. Discord Developer Portal setup

1. Go to https://discord.com/developers/applications and click **New Application**.
2. Open **Bot**. Under **Privileged Gateway Intents** leave everything **off**.
   KITT only uses the `guilds` and `voice_states` intents, which are not privileged.
3. Copy the **bot token** (Bot → Reset Token). It goes in `DISCORD_TOKEN`.
4. Under **OAuth2 → URL Generator**:
   - Scopes: check **`bot`** and **`applications.commands`**
     (without `applications.commands` the slash commands cannot be registered)
   - Bot Permissions: check only **View Channel** and **Connect**
5. Open the generated URL and invite the bot to your server.

Do **not** grant Administrator. KITT does not need it.

## 2. Configuration

Copy `.env.example` to `.env` and fill in the three values:

```
DISCORD_TOKEN=your_bot_token_here
GUILD_ID=your_server_id_here
VOICE_CHANNEL_ID=your_voice_channel_id_here
```

To get the IDs: enable Developer Mode in Discord (User Settings → Advanced),
then right-click the server / the voice channel and choose **Copy Server ID** /
**Copy Channel ID**.

All three are validated at startup. Problems are reported together, and values
are never echoed back, so a token pasted into the wrong variable cannot leak
into the logs. The token is also redacted from all log output.

## 3. Local installation

Requires **Python 3.10 or newer** (3.12 recommended).

```bash
cd KITT
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

`requirements.txt` asks for `discord.py[voice]>=2.7.1`. This matters: since
March 2026 Discord requires its DAVE end-to-end voice encryption, which
discord.py supports from 2.7. Older versions cannot join voice at all. The
`[voice]` extra installs PyNaCl and `davey`; KITT checks for both at startup.

Typical console output:

```
[INFO] KITT starting...
[INFO] Synced 4 slash command(s).
[INFO] Logged in as KITT#1234
[INFO] Connected to server.
[INFO] KITT is ready.
[INFO] Not connected to the configured Voice Channel (trigger: startup).
[INFO] Joined Voice Channel. (startup)
```

(The last two lines can appear before "KITT is ready." — they run concurrently.)

## 4. Slash commands

Commands are registered to your server only (`GUILD_ID`). All replies are ephemeral.

| Command      | What it does                                                                                              | Who can use it                      |
|--------------|-----------------------------------------------------------------------------------------------------------|-------------------------------------|
| `/status`    | Online state, voice connection, current channel, uptime, gateway latency, reconnect status / last error  | Everyone                            |
| `/ping`      | Gateway latency and API response time                                                                      | Everyone                            |
| `/reconnect` | Forces one reconnect attempt now, skipping any backoff wait. Does nothing if already connected correctly. | Server administrators and bot owner |
| `/help`      | Lists the commands and who may use them                                                                    | Everyone                            |

"Bot owner" means the owner of the application in the Developer Portal, or any
member of its team.

## 5. Railway deployment

1. Push this project to a GitHub repo.
2. Create a new Railway project from that repo.
3. In **Variables**, add `DISCORD_TOKEN`, `GUILD_ID`, `VOICE_CHANNEL_ID`.
4. Railway reads the `Procfile` and runs `python main.py` as a background worker.
   No web server, port, or healthcheck is needed. If the dashboard asks for a
   start command, use `python main.py`.
5. Deploy and check the deploy logs for the startup lines above.

Notes:

- **Builder.** Railway now defaults new services to Railpack, which ignores
  `nixpacks.toml`; that is expected to be fine, since PyNaCl and `davey`
  normally install from prebuilt wheels (check the first build log to confirm).
  `nixpacks.toml` only matters if the service is set to the
  legacy Nixpacks builder. The Python version is pinned in `.python-version`,
  which both builders honour.
- **Restart policy.** KITT recovers from voice and gateway problems by itself,
  but if the *process* exits (for example Discord's login endpoint is
  unreachable at boot) it relies on Railway to restart it. In the service's
  Settings, set the restart policy to **Always** (or On Failure with a high
  retry count) so it keeps trying.
- Railway stops containers with SIGTERM. KITT handles it by leaving voice and
  closing the gateway cleanly.

## 6. How reconnection works

- **One supervisor task** owns all reconnect decisions. Gateway events
  (`on_ready`, `on_resumed`, voice-state updates), the 30 s watchdog and
  `/reconnect` never start their own retry loops. They wake the supervisor or
  request a single forced attempt.
- **One lock** serializes every connect / move / disconnect, so duplicate
  connections and competing attempts cannot happen. A second `/reconnect` while
  one is running is told so instead of queued.
- **One retry state**: failures count and next-attempt time, with exponential
  backoff plus jitter (about 5 s doubling up to a 5 minute cap). It resets only
  after the connection has been verified healthy.
- **Wrong channel** (someone moved the bot): detected and corrected with
  `move_to`, without a disconnect/reconnect cycle.
- **discord.py's own voice reconnect** is given a 20 s grace period before KITT
  discards its voice client, so the two mechanisms don't fight.
- **Stale or half-open voice clients** left by failed attempts are cleaned up,
  which otherwise causes endless "Already connected" errors.
- **Shutdown** cancels the supervisor, leaves voice, then closes the gateway.

## 7. Testing it manually

- **Disconnect the bot** from the voice channel in Discord. It logs
  `Disconnected from Voice Channel unexpectedly. Reconnecting...` and rejoins
  within a few seconds.
- **Move the bot** to another voice channel. It moves itself back.
- **Remove Connect** from the bot on the channel. Watch `/status` and the logs:
  a clear permission error, then retries with growing delays (not a tight
  loop). Restore the permission and it rejoins on the next retry, or run
  `/reconnect` to skip the wait.
- **Restart** the process or redeploy. It rejoins with no manual action.
- **Non-admin**: `/reconnect` as a normal member returns a permission message.

## 8. Running the automated tests

```bash
python -m unittest discover -s tests -t . -v
```

The suite covers config validation, backoff, the voice manager (concurrency,
backoff gating, grace period, wrong channel, timeouts, shutdown, event storms),
command output, the error handler, owner detection, event handling, startup
failure paths and a real SIGTERM shutdown.

If the real `discord.py` is installed, the voice-manager and config tests run
against its real exception classes. The command/bot tests need a small
stand-in for the discord API (`tests/_discord_stub.py`) and are skipped when the
real library is present. The stand-in mirrors discord.py's shape but is **not**
discord.py: see the limitations below.

## 9. Limitations

- **Not 100% uptime.** KITT recovers from recoverable failures. It cannot
  help during a Discord or Railway outage, and a deleted channel, removed
  permission or revoked token needs a human; KITT keeps retrying (capped at one
  attempt per ~5 minutes) and reports the reason in logs and `/status`.
- A full voice channel (user limit) or Discord rejecting the connection will
  show up as repeated connect failures.
- Only regular voice channels are supported, not Stage channels.
- If the process cannot even log in at startup (e.g. no network), it exits and
  depends on the platform restart policy (see Railway notes).
