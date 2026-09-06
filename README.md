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
| At least one AI key | free tier | Gemini, Groq, Cerebras, OpenRouter, Mistral or GitHub Models — see below |
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

## Step 3b — Add backup AI providers (recommended)

One free tier is one 429 away from a silent bot. The bot takes a list of
providers and uses the first one that answers, so add a couple of spares — each
is a key in an env variable, no code changes. All of them are free and none ask
for a card:

| Provider | Key from | Notes |
|---|---|---|
| `GEMINI_API_KEY` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | best quality of the free ones |
| `GROQ_API_KEY` | [console.groq.com/keys](https://console.groq.com/keys) | ~30 req/min, extremely fast |
| `CEREBRAS_API_KEY` | [cloud.cerebras.ai](https://cloud.cerebras.ai) | ~30 req/min, ~1M tokens/day |
| `OPENROUTER_API_KEY` | [openrouter.ai/keys](https://openrouter.ai/keys) | one key, many `:free` models |
| `MISTRAL_API_KEY` | [console.mistral.ai/api-keys](https://console.mistral.ai/api-keys) | needs a phone number — see note |
| `NVIDIA_API_KEY` | [build.nvidia.com](https://build.nvidia.com) | NVIDIA NIM, ~80 models |
| `HF_TOKEN` | [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) | Hugging Face Inference Providers |

**GitHub Models is gone.** It was fully retired on 30 July 2026 and now answers
`HTTP 410`. It is not in the default order; if `GITHUB_MODELS_TOKEN` is still
set the bot ignores it and says so at startup. Revoke that token — it buys you
nothing and a classic PAT usually carries repo access.

**Mistral is the fiddly one.** "You don't have access to this application"
means the account exists but the free tier isn't switched on yet: Mistral gates
it behind **phone (SMS) verification**, and you then have to activate the free
**Experiment** plan in the console before the API Keys page will open. No card,
but a real phone number. If you'd rather not hand one over, skip it — the other
five need nothing but an email, and any two of them already make the bot
effectively outage-proof.

Order is `AI_ORDER`, default `gemini,groq,cerebras,openrouter,nvidia,huggingface,mistral`.
Providers without a key are skipped silently, so you can add them one at a
time. The log names whoever ends up answering:

```
-> gemini 429 on gemini-3.8-flash: quota exceeded
-> handing over to the next provider instead of waiting
-> groq 200 in 0.6s, 74 chars
-> answered by groq (the ones before it were busy)
```

**Model names die constantly** on free tiers, and every provider phrases it
differently. The bot handles that itself: on a "model does not exist" error it
takes the replacement slug straight out of the error message if the provider
offered one, otherwise it asks `/models` what is actually being served and
picks the best conversational model — deliberately skipping the guard, whisper,
embedding and moderation models that litter those catalogues. It then keeps the
new model for the session and logs it:

```
-> groq: llama-3.1-8b-instant is gone, switching to llama-3.3-70b-versatile
   (set GROQ_MODEL to keep it)
```

When a backup exists, Gemini stops retrying almost immediately — switching
costs half a second, waiting out a backoff costs the customer. The group judge
uses the same chain, so it survives an outage too. `/status` lists the live
order.

Conversation history is stored provider-neutrally and converted per call, so
switching providers mid-conversation loses nothing: if Gemini answers one
message and Groq the next, Groq sees the whole thread — including Gemini's
reply — and the identical persona prompt. The person on the other end cannot
tell that anything changed.

Everything except Gemini is OpenAI-compatible, so any other endpoint of that
shape can be dropped into `PROVIDER_CATALOGUE` in five lines.

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

Three owner-only commands in that private chat:

| Command | What it does |
|---|---|
| `/start` | proves the bot is running |
| `/status` | model in use, providers, business connections, group settings |
| `/check` | sends one real request to **every** configured AI provider and reports which keys work, which model answered, and how long it took |

`/check` is the fast way to find a dead key. It names the HTTP error the
provider returned, and if a model has been decommissioned it asks the provider
what it does serve and tells you exactly what to set:

```
2 of 4 providers answered. The working ones cover for the rest.

OK    gemini      gemini-3.8-flash  (1.2s)
OK    groq        llama-3.3-70b-versatile  (0.4s)
FAIL  cerebras    HTTP 404: model `llama-3.3-70b` was decommissioned
                  - try CEREBRAS_MODEL=llama-4-scout-17b
FAIL  openrouter  HTTP 401: No auth credentials found
```

Anyone who isn't `OWNER_ID` gets "This bot is private." and no diagnostics.
The same check runs standalone as `python check_keys.py`.

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

## Step 6b — Group chats (optional)

Telegram Business only covers 1:1 chats. For a group the bot joins as an
ordinary member, and posts under its own name (`@TieChat_bot`), not yours.

1. Add the bot to the group like any other member.
2. That's it. He always answers when the room involves him, and may join a
   conversation that isn't about him when he has something worth adding.

   He always answers when:
   - somebody **@mentions it**, or **replies to one of its messages** — these
     always go through, no judging and no cooldown;
   - somebody **calls him by name** — "Илья", "Илюха", or just "бот", which is
     what people in a group tend to call him;
   - **somebody is talking about him** — asking where he went, wondering why
     he's quiet, referring to him in the third person, arguing with something
     he said.

   Beyond that, a second cheap model reads the last dozen lines and decides
   whether he has a reason to join in: he knows something about the subject,
   somebody said something plainly wrong, a question is hanging unanswered, or
   the room is joking and a line would land. Being merely interested is
   explicitly *not* a reason — everyone could have an opinion, that isn't one.

   The judge is also told how many seconds he has been quiet and instructed to
   weigh it: two uninvited lines in a row is where a chat member becomes a
   nuisance. That replaces the old fixed cooldown with something that can read
   the room. Set `GROUP_JOIN_TOPICS=false` to go back to answering only when
   addressed or discussed.

There is no keyword list any more — the judge does that job better. A name in
the text is not a trigger on its own either, it is a *signal handed to the
judge*: "бот, расскажи анекдот" gets an answer, "нам нужен бот для склада" does
not, and a regex cannot tell those apart.

Replies land as ordinary messages, not quoted — he is talking to the room, not
filing a ticket. In a group he is also allowed to write more than one line:
two or three sentences, or a short riff when the subject deserves it.

**Group replies go out immediately** — no read pause, no typing indicator. That
pacing exists for a 1:1 chat, where somebody is visibly answering *you*; in a
room a 25-second pause just means the conversation has moved on without him.
Set `GROUP_DELAY=true` if you want it there anyway.

A newer message in a group does **not** cancel a reply in progress either —
other people talking is the normal state of a room, not somebody correcting
themselves. (In 1:1 chats it still does.)

There is no cooldown by default (`GROUP_COOLDOWN=0`) — there is nothing to
ration when he only answers people who addressed him. Set it above zero if you
want enforced silence between his replies anyway.

**Cost control**, because the judge is a second API call: it never runs on
`@mentions` or replies (already unambiguous), never on messages under
`GROUP_JUDGE_MIN_CHARS`, never more than `GROUP_JUDGE_MAX_PER_MIN` times a
minute per group, and — the big one — never when the message has no name in it
*and* he hasn't appeared in the last `GROUP_JUDGE_RECENT_TURNS` lines, since
nobody can be referring to a man who isn't in the conversation. It also runs on a lite
model with its own quota. In practice a busy group costs a handful of small
calls a minute, not one per message.

If the judge call fails, the fallback is deliberately narrow: he answers if he
was called by name, and stays quiet otherwise. A Gemini hiccup makes him
reserved, never chatty.

It remembers the whole conversation either way, so when you do call on it, it
knows what was being discussed. Each line it sees is labelled with who said it.

### Privacy mode — required for keywords

By default Telegram delivers **only** commands, `@mentions` and replies-to-the-bot
into a group. Ordinary chatter never reaches the bot at all, so keyword triggers
(and `GROUP_REPLY_ALL`) simply never fire, and **nothing appears in the log** to
explain it — the update was never sent.

To let it hear the room:

1. @BotFather → `/setprivacy` → pick the bot → **Disable**.
2. **Remove the bot from the group and add it back.** The setting is captured
   when the bot joins; without the re-join nothing changes.

The bot checks this at startup and warns loudly if it is still on:

```
WARNING PRIVACY MODE IS ON: Telegram is not delivering ordinary group
        messages to this bot, so keyword triggers will never fire.
```

`/status` shows the same thing. (Making the bot a group admin also works, but
disabling privacy is the cleaner route.)

To limit it to certain groups, put their chat IDs in `GROUP_ALLOWLIST` (the ID
appears in the log as `group -1001234...` the first time anyone writes).

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
| `AI_ORDER` | `gemini,groq,cerebras,openrouter,nvidia,huggingface,mistral` | provider order; those without a key are skipped |
| `GEMINI_MODEL` | `gemini-3.8-flash` | falls back to a working flash model if your key can't use it |
| `GROQ_MODEL` etc. | see table above | per-provider model override |
| `HISTORY_TURNS` | `20` | messages of context kept per chat |
| `REPLY_COOLDOWN` | `2` | seconds between replies in one chat |
| `IGNORE_USER_IDS` | — | user IDs that never get an auto-reply |
| `QUOTE_REPLIES` | `false` | `true` makes replies quote the customer's message instead of arriving as plain ones |
| `READ_MIN` / `READ_MAX` | `3` / `12` | silent pause before the typing indicator appears — picking up and unlocking the phone |
| `READ_CPS` | `25` | reading speed, chars/second, added to that pause based on the incoming message |
| `READ_CAP` | `40` | hard ceiling on the silent pause |
| `TYPING_CPS` | `5` | typing speed, chars/second — about 45 words per minute |
| `TYPING_MIN` / `TYPING_MAX` | `2` / `45` | floor and ceiling on that pause, in seconds |
| `WORKERS` | `4` | chats answered in parallel; `1` turns threading off |
| `OWNER_ID` | — | **your Telegram user id — set this**, see "Locking it to you" below |
| `GROUP_AUTO_LEAVE` | `false` | `true` makes the bot leave groups you are not in, instead of ignoring them |
| `GROUPS_ENABLED` | `true` | answer in group chats the bot has been added to |
| `GROUP_REPLY_ALL` | `false` | `true` answers every group message, not just mentions and replies |
| `GROUP_ALLOWLIST` | — | comma-separated group chat IDs; empty means all groups |
| `GROUP_TRIGGER` | `context` | `context` (model judges) or `all` |
| `GROUP_JUDGE_MODEL` | auto (lite) | model used for the speak/stay-quiet decision |
| `GROUP_JUDGE_TURNS` | `12` | how many recent lines the judge sees |
| `GROUP_JUDGE_MIN_CHARS` | `10` | shorter messages are never judged |
| `GROUP_JUDGE_MAX_PER_MIN` | `8` | ceiling on judge calls per group per minute |
| `GROUP_NAMES` | built-in list | names he answers to; exact words, no stemming |
| `GROUP_COOLDOWN` | `0` | enforced silence between his group replies; 0 = none |
| `GROUP_JOIN_TOPICS` | `true` | may join conversations that aren't about him |
| `GROUP_JUDGE_RECENT_TURNS` | `6` | only with `GROUP_JOIN_TOPICS=false`: how recently he must have spoken for an unnamed message to be judged |
| `GROUP_DELAY` | `false` | `true` applies the 1:1 read/typing pauses in groups as well |
| `MAX_MESSAGE_AGE` | `3600` | ignore messages older than this (seconds) when waking from sleep |
| `TEMPERATURE` | `1.0` | lower = drier and more predictable |
| `MAX_OUTPUT_TOKENS` | `2048` | covers thinking **and** the answer — below ~1024 Gemini 3 returns nothing |
| `GEMINI_THINKING_LEVEL` | `low` | `minimal`/`low`/`medium`/`high`, or empty for the model default |
| `GEMINI_TIMEOUT` | `45` | seconds before a Gemini call is abandoned and retried |

---

## Locking it to you

**A bot with Secretary Mode on is not private by default.** Anyone who knows
`@YourBot` can add it to *their* Telegram Business account under Chatbots, and
it will happily answer *their* customers — in your voice, spending your free
Gemini quota, until the quota runs out and your own bot stops working.

Set one variable to close that:

```
OWNER_ID=191609600
```

Your user id is already in your logs — the `owner=` field of the line
`business connection ... owner=191609600`. Or message @userinfobot.

With `OWNER_ID` set the bot:

- serves **only** business connections belonging to you, and logs a `REFUSED:`
  warning naming anyone who tries;
- answers in a group **only if you are a member of it** — checked via
  `getChatMember` and cached for an hour. Set `GROUP_AUTO_LEAVE=true` to make
  it walk out of strangers' groups rather than sit there silently;
- replies to a direct message from anyone else with "This bot is private." and
  refuses `/status`, so no one else sees your connection IDs.

It fails closed: if it cannot establish whose business connection a message
came through, it stays quiet.

### The rest of the surface

- **The token is the bot.** Anyone holding it controls it completely — no
  `OWNER_ID` helps. Keep it out of git (there's a `.gitignore`), out of
  screenshots, and revoke it via @BotFather → `/mybots` → API Token if it leaks.
- **Keep the repo private.** Nothing secret is in the code, but there's no
  reason to publish it.
- **Restrict the Gemini key** at
  [aistudio.google.com/apikey](https://aistudio.google.com/apikey) — and rotate
  it if it was ever pasted anywhere public.
- **Stop strangers adding the bot to groups at all**: @BotFather →
  `/setjoingroups` → Disable. Only do this if you don't want the group feature.

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
- Holds the unsent reply until the typing pause is over, so it can still be
  called off: deleting the message cancels it silently, editing it starts the
  answer again from the new text, and a newer message supersedes the old one.

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
| Log stops at `-> answering...`, no reply | Gemini call hanging or starved. Check `MAX_OUTPUT_TOKENS` ≥ 1024 and `GEMINI_THINKING_LEVEL=low` |
| `gemini 200 … but no text (finishReason=MAX_TOKENS)` | Thinking ate the whole budget. The bot retries automatically; raise `MAX_OUTPUT_TOKENS` if it persists |
| `gemini 429`, replies slow or missing | Free-tier quota, counted **per model**. The bot now switches to a lighter model at once and returns to the primary after `FALLBACK_MINUTES`. If it happens constantly, set `GEMINI_MODEL=gemini-3.5-flash-lite` — lite tiers have far more headroom |
| Duplicate replies | Two copies of the bot running on one token |
| Render: "no open ports detected" | Instance type or start command wrong — it must be a Web Service running `python bot.py` |
| Render: bot answers, then goes quiet after ~15 min | Free Web Service spun down. Add an uptime pinger |
