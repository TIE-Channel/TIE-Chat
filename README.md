# Telegram Business auto-responder (Gemini, free)

A small Python bot that answers your Telegram Business customers automatically:
replies in whatever language they wrote in, remembers the conversation, and
talks in a dry, irreverent stand-up voice you can change in one env variable.

No webhook, no public URL, no framework — just long polling and two HTTP APIs.

---

## What you need

| Thing | Cost | Where |
|---|---|---|
| Telegram **Premium** on your personal account | paid | required by Telegram for Business mode — there is no free workaround |
| A bot token | free | @BotFather |
| A Gemini API key | free tier | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| A host | free tier | Railway / Render / Fly.io |

---

## Step 1 — Create the bot

1. Open [@BotFather](https://t.me/BotFather) in Telegram.
2. Send `/newbot`, pick a name and a username ending in `bot`.
3. Copy the token it gives you (looks like `8123456789:AAH...`).

## Step 2 — Turn on Secretary Mode for the bot

This is the step everybody misses. Without it, Telegram will not let you attach
the bot to your business account.

1. In @BotFather send `/mybots`.
2. Pick your bot → **Bot Settings** → **Secretary Mode** → **Turn on**.

> BotFather used to call this **Business Mode**. Same setting, renamed — if a
> tutorial tells you to look for "Business Mode", tap **Secretary Mode**.

## Step 3 — Get a free Gemini key

1. Go to [aistudio.google.com/apikey](https://aistudio.google.com/apikey).
2. **Create API key** → pick a project → copy it. No credit card needed.

The free tier is rate-limited per minute and per day. If you get more traffic
than that, the bot logs a rate-limit warning, backs off, and retries.

## Step 4 — Run it once locally to check it works

```bash
pip install -r requirements.txt

export TELEGRAM_BOT_TOKEN="8123456789:AAH..."
export GEMINI_API_KEY="AIza..."

python bot.py
```

You should see `logged in as @your_bot` and `polling for business messages...`.

Sanity checks:

```bash
python list_models.py     # which Gemini models your key can use
```

Message your bot directly with `/start` — it should answer. That only proves it
is alive; the real work happens after step 5.

## Step 5 — Connect it to Telegram Business

On your **personal** Telegram account (the one with Premium):

1. **Settings → Telegram Business → Chatbots**
2. Type your bot's username, select it.
3. Choose which chats it handles (all chats / only new ones / exclude specific
   people). Start with a narrow set while you test.
4. Make sure **"Reply to messages"** permission is enabled — without it the bot
   can read but not answer, and it will log `no reply rights`.

Now message your business account from a second Telegram account. The reply
arrives as if you sent it yourself.

## Step 6 — Deploy so it runs 24/7

### Railway (easiest)

1. Push this folder to a GitHub repo.
2. [railway.app](https://railway.app) → **New Project → Deploy from GitHub repo**.
3. **Variables** → add `TELEGRAM_BOT_TOKEN` and `GEMINI_API_KEY`.
4. It picks up the `Dockerfile` automatically. Deploy. Done.

Railway's free credit runs out monthly; a polling bot is cheap but not free
forever.

### Render

Render's free plan covers **Web Services only** — Background Workers start at a
paid tier. A polling bot has nothing to serve, but this one opens a health
endpoint when `PORT` is set (Render sets it automatically), so it can run as a
free Web Service.

1. Push to GitHub (private repo is fine — connect your GitHub account to Render).
2. [render.com](https://render.com) → **New +** → **Web Service** → pick the repo.
3. Language **Python 3**, Branch `main`, Instance Type **Free**.
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `python bot.py`
4. **Environment Variables** → add `TELEGRAM_BOT_TOKEN` and `GEMINI_API_KEY`.
5. **Create Web Service**. Watch the log for `polling for business messages...`.

**Then stop it falling asleep.** Render spins a free Web Service down after 15
minutes with no inbound HTTP traffic, and a sleeping bot stops polling. Point a
free uptime pinger ([cron-job.org](https://cron-job.org),
[UptimeRobot](https://uptimerobot.com)) at your `https://<name>.onrender.com/`
URL every 10 minutes. One always-on service uses ~730 of the 750 free instance
hours per month, so keep it as the only free service in that workspace.

If you'd rather not babysit it, a paid **Background Worker** ($7/mo) needs no
pinger and no health endpoint — same repo, same two variables.

### Fly.io

```bash
fly launch --no-deploy
fly secrets set TELEGRAM_BOT_TOKEN=... GEMINI_API_KEY=...
fly deploy
```

Then in `fly.toml` remove the `[http_service]` block and set `min_machines_running`
so it never sleeps — a sleeping machine stops polling.

> Only ever run **one** instance. Two processes calling `getUpdates` on the same
> token fight over updates and you get duplicate or missing replies.

---

## Step 7 — Make it sound like you

Everything lives in the `PERSONA` env variable. Leave it unset for the built-in
voice, or override it:

```bash
export PERSONA="You are the auto-reply for Dmitrii's studio.
Reply in the customer's language. Two sentences max. Dry humour, no emoji.
Opening hours Mon-Fri 10-18. Never quote prices - say Dmitrii will confirm."
```

Put your real business facts in there — hours, address, what you do, what you
don't do. The default prompt explicitly forbids inventing prices, dates and
promises, which is the failure mode that actually costs you money.

Other knobs, all optional (see `.env.example`):

| Variable | Default | Meaning |
|---|---|---|
| `GEMINI_MODEL` | `gemini-3.8-flash` | falls back to a working flash model if your key can't use it |
| `HISTORY_TURNS` | `20` | messages of context kept per chat |
| `REPLY_COOLDOWN` | `2` | seconds between replies in one chat |
| `IGNORE_USER_IDS` | — | user IDs that never get an auto-reply |
| `MAX_MESSAGE_AGE` | `3600` | ignore messages older than this (seconds) when waking from sleep |
| `TEMPERATURE` | `1.0` | lower = drier and more predictable |
| `MAX_OUTPUT_TOKENS` | `800` | reply length ceiling |

---

## How it works

- Polls `getUpdates` for `business_connection` and `business_message` updates.
- On `business_connection` it caches the owner's user ID and the bot's rights.
- On `business_message` it skips anything **you** sent (both your side and the
  customer's side of the chat arrive as the same update type — this is the part
  that bites people), skips groups, non-text and bots.
- Sends the last `HISTORY_TURNS` messages plus a system prompt to Gemini.
- Replies with `sendMessage` carrying `business_connection_id`, which is what
  makes the message appear to come from you.
- Filters out Gemini "thought" parts, retries on 429/5xx, splits replies over
  4096 characters.

## Known limits

- **History is in memory.** A redeploy or crash forgets ongoing conversations.
  Fine for support chats; add Redis or SQLite if you need it durable.
- **Text only.** Photos, voice notes and stickers are ignored rather than
  guessed at.
- **It answers everyone, instantly.** Telegram's own Chatbots screen is where
  you exclude specific chats. If you'd rather it only fire outside working
  hours, that's a few lines in `handle_business_message`.
- The reply looks like it came from you personally. Tell customers there's a bot
  in the loop if that matters where you are — some jurisdictions require it.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Bot doesn't appear in the Chatbots list | Secretary Mode is off in @BotFather (step 2) |
| Bot can read but replies silently fail | Telegram limits some actions to private chats with a *recent* incoming message — send it a fresh one |
| `no reply rights` in the log | "Reply to messages" toggle off in Telegram Business settings |
| No `business_message` updates at all | Chat is excluded in the Chatbots screen, or account has no Premium |
| `gemini 404` | Model name not available to your key — run `list_models.py` |
| Duplicate replies | Two copies of the bot running on one token |
| Render: "no open ports detected" | Instance type or start command wrong — it must be a Web Service running `python bot.py` |
| Render: bot answers, then goes quiet after ~15 min | Free Web Service spun down. Add an uptime pinger |
