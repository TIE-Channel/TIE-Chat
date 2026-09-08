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
| `CEREBRAS_API_KEY` | [cloud.cerebras.ai](https://cloud.cerebras.ai) | free tier covers the *small* models only |
| `OPENROUTER_API_KEY` | [openrouter.ai/keys](https://openrouter.ai/keys) | one key, many `:free` models |
| `MISTRAL_API_KEY` | [console.mistral.ai](https://console.mistral.ai) | needs SMS verification; ~1 req/sec |
| `NVIDIA_API_KEY` | [build.nvidia.com](https://build.nvidia.com) | NVIDIA NIM, ~80 models |
| `HF_TOKEN` | [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) | Hugging Face Inference Providers |

**GitHub Models is gone.** It was fully retired on 30 July 2026 and now answers
`HTTP 410`. It is not in the default order; if `GITHUB_MODELS_TOKEN` is still
set the bot ignores it and says so at startup. Revoke that token — it buys you
nothing and a classic PAT usually carries repo access.

**Mistral is the fiddly one.** There is no separate free plan to subscribe to —
free mode is simply the default state of a new account. What gates it is
**activating Studio**, which needs email *and* **phone (SMS) verification**.
Until that is done the console shows "You don't have access to this
application" and a valid-looking key answers `429 rate_limited` on the very
first request.

Free mode is also genuinely tight — on the order of one request per second —
so treat Mistral as an emergency spare, not a workhorse. No card is needed, but
a real phone number is. If you'd rather not hand one over, skip it: the others
need nothing but an email, and any two of them already make the bot
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
takes the replacement slug out of the error message if the provider offered a
useful one, then asks `/models` what is actually being served and ranks the
candidates — deliberately skipping the guard, whisper, embedding and moderation
models that litter those catalogues, and on OpenRouter keeping only `:free`
slugs. It tries them in order, because a listed model is not a promise: free
keys are routinely refused models the catalogue advertises. A `402 Payment
required` is treated the same way, except the remaining candidates are then
tried smallest first — free tiers give away the small models and charge for the
large ones. The first model that answers is kept for the session and logged:

```
-> groq: llama-3.1-8b-instant is gone, switching to llama-3.3-70b-versatile
   (set GROQ_MODEL to keep it)
```

**Models are ranked, not just providers.** At startup the bot asks every
provider what it serves and builds one ladder across all of them, ordered by
likely quality — parameter count, model family, version, minus penalties for
`lite`/`mini`/`nano` and for chain-of-thought models that are slow and wordy in
a chat. Replies always use the best rung still available:

```
model ladder (7 rungs): groq/llama-3.3-70b-versatile,
  openrouter/meta-llama/llama-3.3-70b-instruct:free, gemini/gemini-3.8-flash, ...
```

When a model hits its own rate limit it rests for `MODEL_COOLDOWN` (10 min) and
the bot steps down one rung — not to a different provider's worst model, but to
the next-cleverest one anywhere. A model that answers 404/402/410 is retired for
the session. As limits reset the ladder climbs back up on its own. `/status`
prints it with the dead and resting rungs marked.

**Slow models sink — but only where it matters.** The bot times every reply and
keeps a rolling average per rung. In a group it asks for speed: a rung that
takes longer than `SLOW_SECONDS` (8) is pushed far down, because a brilliant
line delivered forty seconds late arrives after the conversation moved on. In a
1:1 chat it asks for quality: the bot is faking a reading-and-typing pause
anyway, so a slow model costs nothing there and keeps its place. The same 550B
model can therefore be first in your DMs and last in a group.

```
-> openrouter/nvidia/nemotron-3-ultra-550b:free took 30s;
   it drops down the ladder for groups
```

Size alone is not treated as quality, either: past ~120B the extra parameters
buy little for a two-line chat reply and cost a lot of queueing on a free tier,
so the curve flattens and then turns down.

**One ladder, walked from both ends.** Replies in all three modes use
`ladder_order()` — cleverest rung first, stepping down as the good ones run out
of quota. The **judge** walks the same ladder the other way: dumbest rung
first, climbing only when one turns out unable to produce a usable verdict. A
yes/no verdict does not need the clever model, and every judge call spent on
one is a call the actual answers no longer have.

The judge sees **every rung** — all models from all providers, same list as
replies. A rung leaves that list only for a reason it will not recover from:
answering with prose instead of a verdict. A 429, a timeout or a 5xx is
*transient*, so it is skipped for that one message and tried again on the next
— it used to be treated as "this model cannot judge" and dropped for the rest
of the session, which quietly shrank the judge's ladder over a long uptime.

`/status` prints both directions:

```
judge (cheapest first): groq/llama-3.1-8b-instant, gemini/gemini-3.5-flash-lite,
  groq/gemma2-9b-it
ladder (best first):
     groq/llama-3.3-70b-versatile             q=165  1s
     openrouter/meta-llama/llama-3.3-70b-instruct:free  q=165
```

`GROUP_JUDGE_MODEL` pins a model to try first. It used to be documented but
silently ignored — it only changed the label in `/status`. Now it actually
reorders the judge's ladder, and if the name is not on the ladder the bot says
so at startup instead of pretending.

**A provider that keeps failing gets parked.** Three failures in a row and it
is skipped entirely for ten minutes (`PARK_AFTER_FAILURES`, `PARK_MINUTES`) —
an exhausted daily quota does not recover in a minute, and trying it first on
every message costs a wasted round trip each time. It is retried automatically
when the parking expires, and `/check` always tests everyone and revives
whoever recovered. `/status` shows who is parked and for how long.

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

## Step 6c — Inline: call him into a chat he is not in (optional)

Business mode covers your own 1:1 chats. Group mode covers rooms he was added
to. Inline mode covers everything else: **any** chat — a group he was never
added to, a channel, someone else's DM, a chat where adding bots is not allowed.

Type his name and the line you want answered, and send it:

```
@yourbot ну и что ты на это скажешь
```

The message goes out **as your own** (with a small "via @yourbot" label), shows
`…` for a second, and fills in with the reply. Typing costs nothing — the model
runs once, at the moment you send.

### Two settings in @BotFather, and it needs both

| Command | Answer |
|---|---|
| `/setinline` | pick your bot, then send a placeholder line, e.g. `что ответить...` |
| `/setinlinefeedback` | pick your bot, then **Enabled** |

The second one is the one everybody misses. Without it Telegram never reports
that the message was sent, so the `…` is posted and simply stays there — no
error in the log, no error on your phone, nothing. The bot watches for exactly
that pattern:

```
offered 9 inline replies and Telegram never said one was sent. If the message
in the chat is stuck on '…', inline feedback is off:
@BotFather -> /setinlinefeedback -> @yourbot -> Enabled.
```

Whether inline mode itself is on *is* readable from the API, so that is checked
at startup:

```
inline mode on (access: shared) - type '@yourbot ...' in ANY chat, even one
this bot was never added to
```

### Why "send first, answer after"

The alternative is to have the reply ready before you tap it — which means
generating while you type. Telegram re-queries the bot on **every keystroke**,
so that costs a request per letter unless you bolt on debouncing and a cache,
and it still has to finish inside Telegram's ~10-second answer window.

Sending first removes all of it. Typing is free; the model runs exactly once
per message you actually send; there is no window to race. The price is the
`…` for a second and the `/setinlinefeedback` switch above.

### Who may use it

`INLINE_ACCESS`:

| Value | Who |
|---|---|
| `owner` | only you |
| `shared` *(default)* | you, plus anyone who is in one of the groups you are in |
| `all` | anybody who knows the username |

**A limit worth knowing:** Telegram never tells a bot *which chat* an inline
query came from. The update carries the sender, the text, and a coarse
`chat_type` — no chat id, by design. So "only in chats I am in" cannot be
enforced against the chat. It can be enforced against the person, which is the
same guarantee from the other end: under `shared`, someone who shares no room
with you gets an empty result and costs you nothing. The verdict is cached for
an hour, so it is one `getChatMember` call per person, not per keystroke.

The list of your groups is learned as people talk in them — the Bot API has no
way to ask "which chats is this bot in" — and it is **stored**, in Redis when
that is configured and in `GROUPS_FILE` otherwise, so a restart does not lock
everyone but you out. `GROUP_ALLOWLIST` seeds it outright. `/status` shows the
count.

**Membership is cached for a week.** Once someone is known to share a group
with you, that verdict lives in Redis under `<prefix>:member:<user id>` for
`INLINE_MEMBER_DAYS` (7) — so it survives a redeploy, and the people who could
use the bot inline last week still can today without a single API call. Most of
the cache fills in for *free*: anyone who writes in one of your groups is
recorded on the spot, because the bot has already established that the room is
yours. A **no** is deliberately cheap to forget — `INLINE_MISS_MINUTES` (15) —
so somebody who joins one of your groups tomorrow is not locked out until next
week.

`INLINE_MAX_PER_MIN` (6) caps each person, so an open bot cannot be drained.

### What he can and cannot see

He gets **only the text you typed after his name**. Inline queries carry no
history, no chat id, not even the name of the chat, so paste in the line you
want answered rather than expecting him to catch up. He is told he was not
there and must not pretend otherwise.

There is deliberately **no memory** between inline calls: with no chat id to
key it on, any memory would be shared across every chat he is summoned into and
would leak one conversation into the next.

Otherwise he plays by the room rules — see the next section.

---

## Step 6d — One line or three: where the rules differ

The persona is written for a **1:1 chat**, because that is the risky case:

> Answer in ONE line. One sentence, occasionally two short ones.
> […] you take the remark apart and not the person: whoever is writing may be
> a customer, and a customer you insulted is a customer lost.

That is the DM behaviour: short, dry, and it never insults the person back.

A **room** — a group he was added to, or an inline summon — overrides both of
those, because a room is not a support desk:

> LENGTH — THERE IS NO LIMIT HERE
> Write as much as you want. One word, one line, or five paragraphs of a rant
> that has been building for years: whatever the subject actually deserves.
>
> TRADING INSULTS — THIS OVERRIDES THE ONE-TO-ONE RULE ABOVE
> These are people who know each other, not a customer chat. If someone comes
> at you, you give as good as you get […]

**Nothing is truncated anywhere any more.** `REPLY_MAX_CHARS` and
`ROOM_MAX_CHARS` are both `0`, and `MAX_OUTPUT_TOKENS` is `4096` — length is the
prompt's business, and cutting at a character count only ever chopped somebody
off mid-sentence, which reads as a bug rather than as a short answer. Anything
past Telegram's 4096-character message limit is split across several messages.

That is why your DMs stay short but are no longer *capped*: the persona asks for
one line, and on the rare occasion he ignores it, the whole thing arrives
instead of the first 1200 characters. The one real cap left is inline, where the
reply is edited into a message that already exists and so cannot be split.

What he still may not do in a room is *format* like a machine: no lists, no
bullet points, no headings, no bold, no emoji. That is about looking like a
person typing in a chat, not about length.

Both places share the same hard limits: nothing about ethnicity, nationality,
religion, gender, sexuality, disability, illness or family; no threats; nothing
sexual; no piling on someone already being dogpiled. He never starts it, gives
one line back per jab, and drops it the moment someone is actually upset.

So: **one line and no comebacks in your DMs; any length and full comebacks in
groups and inline.** In the code that is `DEFAULT_PERSONA` versus `ROOM_RULES`,
which `GROUP_NOTE` and `INLINE_NOTE` both append.

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
| `UPSTASH_REDIS_REST_URL` / `_TOKEN` | — | Upstash Redis; takes priority over the file |
| `REDIS_PREFIX` | `tgbot` | key prefix, so one database can serve several bots |
| `HISTORY_TTL_DAYS` | `30` | forget a chat nobody has touched in this long; 0 = never |
| `HISTORY_FILE` | `history.json` | fallback file; empty = memory only |
| `HISTORY_SAVE_EVERY` | `20` | seconds between saves (a burst is batched into one write) |
| `HISTORY_MAX_CHATS` | `300` | liveliest chats kept in the file |
| `REPLY_COOLDOWN` | `0` | seconds between replies in one chat; off by default |
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
| Nothing at all in the log, however much you write, and `Conflict: terminated by other getUpdates request` | **Two instances on one token.** Telegram hands each message to exactly one poller and it is not this one. Usually the previous Render deploy still shutting down — wait a minute. If it persists: a second Render service on the same `TELEGRAM_BOT_TOKEN`, or a copy still running on your laptop. Stop one. |
| Bot doesn't appear in the Chatbots list | Secretary Mode is off in @BotFather (step 2) |
| Bot can read but replies silently fail | Telegram limits some actions to private chats with a *recent* incoming message — send it a fresh one |
| `no reply rights` in the log | "Reply to messages" toggle off in Telegram Business settings |
| Typing `@yourbot ...` in a chat finds nothing | Inline mode is off — `/setinline` in @BotFather (step 6c) |
| The inline message is posted but stays on `…` forever | Inline feedback is off — `/setinlinefeedback` → Enabled (step 6c) |
| Inline result is empty, with a "This bot is private." button | `INLINE_ACCESS=shared` and that person shares no known group with you — `/status` shows how many groups are known |
| Inline works for you but not for anyone else | No groups known yet. Let someone write in one of yours, or set `GROUP_ALLOWLIST` |
| No `business_message` updates at all | Chat is excluded in the Chatbots screen, or account has no Premium |
| `gemini 404` | Model name not available to your key — run `list_models.py` |
| Log stops at `-> answering...`, no reply | Gemini call hanging or starved. Check `MAX_OUTPUT_TOKENS` ≥ 1024 and `GEMINI_THINKING_LEVEL=low` |
| `gemini 200 … but no text (finishReason=MAX_TOKENS)` | Thinking ate the whole budget. The bot retries automatically; raise `MAX_OUTPUT_TOKENS` if it persists |
| `gemini 429`, replies slow or missing | Free-tier quota, counted **per model**. The bot now switches to a lighter model at once and returns to the primary after `FALLBACK_MINUTES`. If it happens constantly, set `GEMINI_MODEL=gemini-3.5-flash-lite` — lite tiers have far more headroom |
| Duplicate replies | Two copies of the bot running on one token |
| Render: "no open ports detected" | Instance type or start command wrong — it must be a Web Service running `python bot.py` |
| Render: bot answers, then goes quiet after ~15 min | Free Web Service spun down. Add an uptime pinger |
