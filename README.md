# KITT

A minimal Discord bot with one job: stay connected to a single voice channel,
24/7, silently. No audio, no music, no dashboard, no database.

## 1. Discord Developer Portal setup

1. Go to https://discord.com/developers/applications and click **New Application**.
2. Under **Bot**, click **Add Bot**.
3. Under **Bot → Privileged Gateway Intents**, leave everything **off**:
   - Presence Intent: off (not used)
   - Server Members Intent: off (not used)
   - Message Content Intent: off (not used)

   This bot only uses the `guilds` and `voice_states` intents, neither of
   which is privileged, so no toggles need to be enabled here.
4. Copy the **bot token** (Bot → Reset Token / Copy) — this goes in `.env` as `DISCORD_TOKEN`.
5. Under **OAuth2 → URL Generator**:
   - Scopes: check `bot`
   - Bot Permissions: check only **View Channel** and **Connect**
   - Copy the generated URL, open it, and invite the bot to your server.

## 2. Required bot permissions

Only two permissions are needed:

- **View Channel**
- **Connect**

Do **not** grant Administrator. The bot does not need it and should not have it.

## 3. .env configuration

Copy `.env.example` to `.env` and fill in the three values:

```
DISCORD_TOKEN=your_bot_token_here
GUILD_ID=your_server_id_here
VOICE_CHANNEL_ID=your_voice_channel_id_here
```

To get `GUILD_ID` and `VOICE_CHANNEL_ID`: enable Developer Mode in Discord
(User Settings → Advanced → Developer Mode), then right-click your server
and the target voice channel and choose **Copy Server ID** / **Copy Channel ID**.

## 4. Local installation

```bash
cd KITT
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

You also need `ffmpeg`-free voice support installed via `discord.py[voice]`
(handled by `requirements.txt`) and PyNaCl, which installs automatically as
part of the `[voice]` extra.

Run it:

```bash
python main.py
```

Expected console output:

```
[INFO] KITT starting...
[INFO] Logged in as KITT#1234
[INFO] Connected to server.
[INFO] Joined Voice Channel.
[INFO] KITT is ready.
```

## 5. Railway deployment

1. Push this project to a GitHub repo (or use the Railway CLI directly).
2. Create a new Railway project from that repo.
3. In Railway → Variables, add:
   - `DISCORD_TOKEN`
   - `GUILD_ID`
   - `VOICE_CHANNEL_ID`
4. Railway will detect the `Procfile` and run the `worker` process:
   ```
   worker: python main.py
   ```
   No web server, no HTTP port, no healthcheck endpoint is needed — this is
   a background worker only. If Railway's dashboard asks for a start
   command, use `python main.py`.
5. Deploy. Check the deploy logs for the same `[INFO]` startup lines shown above.

## 6. How to test auto-reconnect

- **Kick the bot from voice manually**: right-click the bot in the voice
  channel in Discord and disconnect it. The bot should log
  `Disconnected from Voice Channel unexpectedly. Reconnecting...` and rejoin
  within a few seconds.
- **Delete/restrict the channel temporarily**: rename permissions to remove
  Connect, then watch the bot log a clear permission error on its retry
  attempts (it will keep retrying with backoff, not spam-loop).
- **Restart the process**: stop and start `python main.py` (or redeploy on
  Railway). On startup it should log `Joined Voice Channel.` again without
  any manual action.
- **Manual command**: run `/reconnect` in your server (as an admin or the
  bot owner) to force an immediate reconnect attempt and confirm the
  ephemeral response.
- **Network blip**: `discord.py`'s built-in `reconnect=True` on
  `channel.connect()` handles brief network drops at the voice-gateway
  level automatically; the watchdog task (runs every 30s) and the
  `on_voice_state_update` handler act as a second layer that catches
  anything the low-level reconnect misses, using exponential backoff
  (5s → up to 5 minutes) so it never rapid-loops.
