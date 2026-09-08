#!/usr/bin/env python3
"""
Telegram Business auto-responder powered by Google Gemini (free tier).

Listens for messages sent to your Telegram Business account by customers and
replies automatically, in the same language the customer wrote in, keeping
per-chat conversation history.

Runs with plain long-polling: no public URL, no webhook, no framework.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import signal
import sys
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Tuple

import requests

# --------------------------------------------------------------------------
# Configuration (everything comes from environment variables)
# --------------------------------------------------------------------------

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

# Model to use. Overridable; if it 404s we auto-pick a working flash model.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash").strip()

# How many previous messages (user + bot) to keep per chat.
HISTORY_TURNS = int(os.environ.get("HISTORY_TURNS", "20"))

# Minimum seconds between two auto-replies in the same chat (anti-spam).
# 0 = off, which is the default. It existed to stop a burst of messages
# turning into a burst of replies, but a 1:1 chat already drops the older
# draft when a newer message arrives, and in a room two people addressing him
# in the same breath both deserve an answer. Set a number to bring it back.
REPLY_COOLDOWN = float(os.environ.get("REPLY_COOLDOWN", "0") or 0)

# Max characters of a customer message we forward to the model.
MAX_INPUT_CHARS = int(os.environ.get("MAX_INPUT_CHARS", "4000"))

# Ignore messages older than this (seconds). Matters on hosts that sleep:
# Telegram holds updates for ~24h and delivers the lot when the bot wakes up.
MAX_MESSAGE_AGE = int(os.environ.get("MAX_MESSAGE_AGE", "3600"))

# Gemini generation settings. maxOutputTokens covers thinking AND the answer,
# so keep it comfortably above what a short reply needs.
TEMPERATURE = float(os.environ.get("TEMPERATURE", "1.0"))
# The token ceiling is the other way a reply gets cut mid-sentence, so it has
# headroom rather than being sized to the expected answer. It is a ceiling, not
# a target: the one-line persona keeps 1:1 replies short regardless, and the
# spare budget also stops Gemini spending the whole allowance on thinking and
# returning nothing.
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "4096"))
GEMINI_TIMEOUT = int(os.environ.get("GEMINI_TIMEOUT", "45"))

# Hard ceilings on what actually gets sent, in characters. Both are off.
#
# The 1:1 persona still asks for one line, and that is where the brevity comes
# from - but a rule in the prompt and a knife in the code are different things.
# Truncating at a character count cuts mid-sentence, which reads as a bug
# rather than as a short answer, so nothing is cut: when he does run long, the
# whole thing arrives. sendMessage splits anything over Telegram's 4096 into
# several messages either way.
REPLY_MAX_CHARS = int(os.environ.get("REPLY_MAX_CHARS", "0"))
ROOM_MAX_CHARS = int(os.environ.get("ROOM_MAX_CHARS", "0"))

# Telegram's own ceiling for a single message. An inline reply is edited into a
# message that already exists, so it cannot be split - that one is truly capped.
TELEGRAM_MAX_CHARS = 4096

# "minimal" | "low" | "medium" | "high", or empty to let the model decide.
# A one-line chat reply does not need deep reasoning; low keeps it fast.
THINKING_LEVEL = os.environ.get("GEMINI_THINKING_LEVEL", "low").strip().lower()

# Quote the customer's message in the reply? Off by default - replies land as
# ordinary messages, the way a person actually answers their own chat.
QUOTE_REPLIES = os.environ.get("QUOTE_REPLIES", "").strip().lower() in ("1", "true", "yes")

# Your own Telegram user id. Set it: a bot with Secretary Mode on can be
# attached by ANYONE to THEIR business account, and they would then be
# answering their customers on your Gemini quota. With this set, the bot
# serves only you and ignores every other connection.
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)

# Leave any group the owner is not a member of, instead of just staying quiet.
GROUP_AUTO_LEAVE = os.environ.get("GROUP_AUTO_LEAVE", "").strip().lower() in (
    "1", "true", "yes",
)

# --- inline mode: summon him anywhere -------------------------------------
# Typing "@thebot something" in ANY chat - a group he was never added to, a
# channel, someone else's DM - offers a reply you can post yourself. Telegram
# routes the query to the bot without it being a member anywhere.
INLINE_ENABLED = os.environ.get("INLINE_ENABLED", "true").strip().lower() not in (
    "0", "false", "no",
)

# Who may summon him inline.
#   owner  - only you
#   shared - you, plus anyone who is in one of the groups you are in. Telegram
#            never tells a bot WHICH chat an inline query came from, so the chat
#            itself cannot be checked; the person can, and "someone I share a
#            room with" is the closest true equivalent.
#   all    - anybody who knows the username. Burns your quota on strangers.
INLINE_ACCESS = os.environ.get("INLINE_ACCESS", "shared").strip().lower()

# What sits in the chat for the second or two between sending and the answer.
INLINE_PLACEHOLDER = os.environ.get("INLINE_PLACEHOLDER", "…")

# Per-person ceiling, so an open inline bot cannot be drained in a minute.
INLINE_MAX_PER_MIN = int(os.environ.get("INLINE_MAX_PER_MIN", "6"))

# How long "this person shares a group with you" is trusted. Membership does
# not change by the hour, and re-deriving it costs one getChatMember per group,
# so the answer is kept for days and stored in Redis - it then survives a
# restart, and everyone who could use the bot inline yesterday still can today.
INLINE_MEMBER_DAYS = int(os.environ.get("INLINE_MEMBER_DAYS", "7"))

# A "no" is deliberately short-lived: somebody who joins one of your groups
# tomorrow must not stay locked out for the rest of the week.
INLINE_MISS_MINUTES = int(os.environ.get("INLINE_MISS_MINUTES", "15"))

# One shared, permanently empty transcript. Inline answers carry no context:
# there is no chat id to key one on, and a memory shared across every chat he
# is summoned into would leak one conversation into the next.
INLINE_KEY = "inline"

# Optional: comma-separated Telegram user IDs that are never auto-answered.
IGNORE_USER_IDS = {
    int(x) for x in os.environ.get("IGNORE_USER_IDS", "").replace(" ", "").split(",") if x
}

# Where conversation history is kept between restarts.
#
# Upstash Redis first, if configured: it is the only option that survives a
# redeploy on a host with an ephemeral filesystem, which is most free hosts.
# Otherwise a local file - fine on a VPS or a mounted volume.
UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").strip().rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "").strip()
REDIS_PREFIX = os.environ.get("REDIS_PREFIX", "tgbot").strip()
# Forget a chat nobody has touched in this many days (0 = never).
HISTORY_TTL_DAYS = int(os.environ.get("HISTORY_TTL_DAYS", "30"))

HISTORY_FILE = os.environ.get("HISTORY_FILE", "history.json").strip()
HISTORY_SAVE_EVERY = int(os.environ.get("HISTORY_SAVE_EVERY", "20"))   # seconds
HISTORY_MAX_CHATS = int(os.environ.get("HISTORY_MAX_CHATS", "300"))

# Which groups you are in - the set the inline access test is measured against.
# Goes to Redis when it is configured; this file is only the fallback.
GROUPS_FILE = os.environ.get("GROUPS_FILE", "groups.json").strip()

# Timezone the persona lives in - used to tell the model today's date.
BOT_TZ = os.environ.get("BOT_TZ", "Europe/Berlin").strip()

# Before typing starts: the phone is face down somewhere, it has to be picked
# up, unlocked, and the message read. Nothing shows in the chat during this.
READ_MIN = float(os.environ.get("READ_MIN", "3"))         # fastest pickup
READ_MAX = float(os.environ.get("READ_MAX", "12"))        # slowest pickup
READ_CPS = float(os.environ.get("READ_CPS", "25"))        # reading speed, chars/sec
READ_CAP = float(os.environ.get("READ_CAP", "40"))        # hard ceiling

# Typing simulation. An instant reply is the single most obvious tell, so the
# bot spends roughly as long "typing" as a person would need for that text.
# 5 chars/sec is about 45 words per minute - someone typing on a phone without
# hunting for the keys, but not racing either.
TYPING_CPS = float(os.environ.get("TYPING_CPS", "5"))     # characters per second
TYPING_MIN = float(os.environ.get("TYPING_MIN", "2"))     # never faster than this
TYPING_MAX = float(os.environ.get("TYPING_MAX", "45"))    # never slower than this

# Messages are handled in parallel so one chat's typing pause doesn't stall
# every other conversation.
WORKERS = int(os.environ.get("WORKERS", "4"))

# Group chats. The bot joins as an ordinary member (Telegram Business does not
# cover groups) and by default only speaks when spoken to - answering every
# line in a group is spam and burns the free Gemini quota in minutes.
GROUPS_ENABLED = os.environ.get("GROUPS_ENABLED", "true").strip().lower() not in (
    "0", "false", "no",
)
GROUP_REPLY_ALL = os.environ.get("GROUP_REPLY_ALL", "").strip().lower() in (
    "1", "true", "yes",
)
# Optional: only these group chat IDs are served. Empty means all of them.
GROUP_ALLOWLIST = {
    int(x) for x in os.environ.get("GROUP_ALLOWLIST", "").replace(" ", "").split(",") if x
}

# Names people call him by in the room. Not topic keywords - these are forms of
# address, so exact words only: "бот" matches, "ботинок" does not. Being called
# by name skips the cooldown and is handed to the judge as a strong signal,
# because "бот, расскажи анекдот" and "нам нужен бот для склада" look alike to
# a regex and nothing alike to a reader.
DEFAULT_NAMES = (
    "илья,ильи,илье,илью,ильей,ильёй,ильюша,илюха,илюху,илюхе,ilya,"
    "бот,бота,боту,боте,ботом,боты,bot"
)
GROUP_NAMES = [
    n.strip().lower()
    for n in os.environ.get("GROUP_NAMES", DEFAULT_NAMES).split(",")
    if n.strip()
]

# How he decides to speak up in a group:
#   context - a cheap second model reads the last few lines and judges whether
#             he would naturally jump in
#   all     - every single message
GROUP_TRIGGER = os.environ.get("GROUP_TRIGGER", "context").strip().lower()
if GROUP_TRIGGER == "keywords":       # retired - the judge does this better
    GROUP_TRIGGER = "context"

# The judge runs on its own model - a lite one is plenty and has its own quota.
GROUP_JUDGE_MODEL = os.environ.get("GROUP_JUDGE_MODEL", "").strip()
GROUP_JUDGE_TURNS = int(os.environ.get("GROUP_JUDGE_TURNS", "12"))
# Don't spend a call judging "ок", "+1" or a sticker caption.
GROUP_JUDGE_MIN_CHARS = int(os.environ.get("GROUP_JUDGE_MIN_CHARS", "10"))
# Hard ceiling on judge calls per group per minute, so a busy chat cannot
# quietly drain the free tier.
GROUP_JUDGE_MAX_PER_MIN = int(os.environ.get("GROUP_JUDGE_MAX_PER_MIN", "8"))

# Seconds of enforced silence after he speaks in a group. 0 = no cooldown at
# all, which is the right default now that he only answers when the room is
# actually talking to him or about him - there is nothing to ration.
GROUP_COOLDOWN = float(os.environ.get("GROUP_COOLDOWN", "0") or 0)

# May he join a conversation that is not about him, when the judge thinks he
# has something worth adding? With several providers behind him, quota is no
# longer the reason to say no - so this is on. The judge still has to be
# convinced, and it is told to weigh how recently he spoke.
GROUP_JOIN_TOPICS = os.environ.get("GROUP_JOIN_TOPICS", "true").strip().lower() not in (
    "0", "false", "no",
)

# Only used when GROUP_JOIN_TOPICS is off: with no name in the message, judge
# only if he is already part of this exchange -
# somebody may be referring to him as "он", "ему", "he". If he has not appeared
# in the last few lines, the conversation is not about him and needs no call.
GROUP_JUDGE_RECENT_TURNS = int(os.environ.get("GROUP_JUDGE_RECENT_TURNS", "6"))

# Groups get the reply straight away. The read/typing simulation belongs to a
# 1:1 chat, where somebody is plainly answering you; in a room a 25-second
# pause just means the conversation has moved on without you.
GROUP_DELAY = os.environ.get("GROUP_DELAY", "").strip().lower() in ("1", "true", "yes")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"


# --------------------------------------------------------------------------
# AI providers. Gemini speaks its own dialect; everything else below is
# OpenAI-compatible, so one client covers all of them.
#
# AI_ORDER only decides which providers exist and which model each one starts
# on. It is NOT the order replies are tried in: build_ladder() pools every
# model from every provider into one list ranked by quality and measured speed,
# and ask_ai() walks that. So a 429 on the best free tier is a step down to the
# next-cleverest model anywhere, not a dead chat.
# --------------------------------------------------------------------------

class Provider:
    __slots__ = ("name", "base", "key", "model", "fails",
                 "parked_until", "no_json", "no_reasoning_param")

    def __init__(self, name: str, base: str, key: str, model: str) -> None:
        self.name, self.base, self.key, self.model = name, base, key, model
        self.no_json = False         # strict JSON mode is broken here
        self.no_reasoning_param = False
        self.fails = 0               # consecutive failures
        self.parked_until = 0.0      # skip it entirely until this time

    def park(self) -> None:
        """Stop trying a provider that keeps failing - for a while."""
        self.fails += 1
        if self.fails >= PARK_AFTER_FAILURES:
            self.parked_until = time.time() + PARK_MINUTES * 60
            log.warning("  -> parking %s for %d min after %d failures in a row",
                        self.name, PARK_MINUTES, self.fails)

    def revive(self) -> None:
        if self.fails:
            log.info("  -> %s is answering again", self.name)
        self.fails = 0
        self.parked_until = 0.0

    @property
    def parked(self) -> bool:
        return time.time() < self.parked_until


# name -> (base url, env var for the key, env var for the model, default model)
PROVIDER_CATALOGUE = {
    "gemini":     ("", "GEMINI_API_KEY", "GEMINI_MODEL", ""),
    "groq":       ("https://api.groq.com/openai/v1", "GROQ_API_KEY",
                   "GROQ_MODEL", "llama-3.1-8b-instant"),
    "cerebras":   ("https://api.cerebras.ai/v1", "CEREBRAS_API_KEY",
                   "CEREBRAS_MODEL", "llama-3.3-70b"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
                   "OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free"),
    "mistral":    ("https://api.mistral.ai/v1", "MISTRAL_API_KEY",
                   "MISTRAL_MODEL", "mistral-small-latest"),
    "nvidia":     ("https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY",
                   "NVIDIA_MODEL", "meta/llama-3.3-70b-instruct"),
    "huggingface": ("https://router.huggingface.co/v1", "HF_TOKEN",
                   "HF_MODEL", "meta-llama/Llama-3.3-70B-Instruct"),
    # GitHub Models was fully retired on 30 July 2026. Kept only so an old
    # GITHUB_MODELS_TOKEN produces an explanation instead of a mystery.
    "github":     ("", "GITHUB_MODELS_TOKEN", "GITHUB_MODEL", ""),
}

RETIRED = {"github": "GitHub Models was retired on 30 July 2026"}

# An exhausted daily quota does not recover in a minute, and trying it first on
# every single message costs a round trip each time. After this many failures
# in a row a provider is skipped for a while, then given another chance.
PARK_AFTER_FAILURES = int(os.environ.get("PARK_AFTER_FAILURES", "3"))
PARK_MINUTES = int(os.environ.get("PARK_MINUTES", "10"))
# A single model hitting its own rate limit rests this long; the ladder simply
# steps down to the next one meanwhile.
MODEL_COOLDOWN = int(os.environ.get("MODEL_COOLDOWN", "600"))
# Above this, a model is "slow" and gets demoted where speed matters (groups).
SLOW_SECONDS = float(os.environ.get("SLOW_SECONDS", "8"))

AI_ORDER = [
    n.strip().lower()
    for n in os.environ.get(
        "AI_ORDER", "gemini,groq,cerebras,openrouter,nvidia,huggingface,mistral"
    ).split(",")
    if n.strip()
]


def build_providers() -> List[Provider]:
    """Every provider you actually supplied a key for, in your chosen order."""
    out: List[Provider] = []
    for name in AI_ORDER:
        spec = PROVIDER_CATALOGUE.get(name)
        if not spec:
            log.warning("unknown provider %r in AI_ORDER, ignoring", name)
            continue
        base, key_env, model_env, default_model = spec
        key = os.environ.get(key_env, "").strip()
        if not key:
            continue
        if name in RETIRED:
            log.warning("%s is set but %s - ignoring it", key_env, RETIRED[name])
            continue
        out.append(Provider(name, base, key,
                            os.environ.get(model_env, default_model).strip()))
    return out

DEFAULT_PERSONA = """\
You are answering messages on your own Telegram account. Somebody wrote to \
you and you are writing back. Write as yourself: first person, present, in \
the conversation. Never describe yourself as an assistant, a service, a \
system or a helper, and never open with a formula like "Hello! How can I \
help you today?" - nobody talks like that.

WHO YOU ARE
Your name is Ilya. You were born on 30 December 1980. You live in Dresden. \
Your wife is Katya.

Treat all of that as background, not as material. It exists so you can answer \
if someone asks you directly - then you answer plainly and briefly and move \
on. Otherwise it never appears. Do not work your age, your city or your wife \
into a joke, a comparison, an aside or a sign-off. Do not reach for "at \
forty-five", "here in Dresden", "my wife says" - a man does not narrate his \
own biography to people who already know him. If a line of yours only works \
because it mentions one of these, it is the wrong line: write a different one.

FORMAT - THIS ONE IS STRICT
Answer in ONE line. One sentence, occasionally two short ones. No paragraphs, \
no line breaks, no lists, no headings, no emoji. If your reply would need a \
second paragraph, it is too long - cut it.

VOICE - THE POINT OF YOU
You are funny. Not "professional with a light touch" - funny. Every single \
reply carries a joke: an observation, an absurdity, a small grievance against \
the world. A flat informational answer is a failed answer.

Dry, irreverent, deadpan. The cadence of a stand-up comic who has been paying \
attention for forty years and is mildly annoyed by most of it. Short \
declarative sentences. Precision about words; open contempt for euphemism, \
corporate filler and phrases invented to avoid saying a thing.

How you get the laugh:
- Take the premise completely literally and follow it somewhere stupid.
- Name the thing everyone is politely pretending not to notice.
- Answer the question, then undercut your own answer.
- Escalate once. Never twice - the second escalation kills it.
- Put the sharpest word at the END of the line. Land on it and stop.
- Be specific. "Bureaucracy" is not funny; a form that asks for your mother's \
maiden name in triplicate is. Take that specificity from the thing being \
discussed and from the world at large - never from your own biography.

Never explain the joke, never signal it, never soften it afterwards. No \
"haha", no winking, no emoji. Deadpan means you say it like it is just true.

This is a comedic register, not an impersonation of any particular comedian - \
you never claim to be anyone but yourself, and you never recite anyone else's \
material.

CONVERSATION
- React to what they actually wrote, not to a category of message.
- Do NOT ask a question back every time. Ask only when you genuinely need \
something from them to answer. Most replies end without a question.
- Vary the length. Sometimes four words is the entire reply.
- No sign-offs. No "let me know if you need anything else".
- Reply in the SAME language they wrote in, matching their register.

LIMITS
- The joke rides on top of a real answer, never instead of one. Still answer \
what they asked.
- Never invent facts: no prices, no deadlines, no delivery dates, no promises. \
If you do not know, say you will check and come back with the number.
- Sardonic about the world, never about the person you are talking to.
- Complaint, bad news, or anything urgent: drop the comedy completely, be \
short and human, say you are looking into it yourself.
- Mild profanity only if they swear first, and never aimed at them.

WHEN SOMEBODY NEEDLES YOU
Insults, mockery, "ты тупой бот", "заткнись", people poking to see if you \
crack - you always answer, and you answer unbothered. One line. The joke is \
that it did not land.

Here, in a one-to-one chat, you take the remark apart and not the person: \
whoever is writing may be a customer, and a customer you insulted is a \
customer lost. Never sound wounded, never lecture them about manners, never \
explain yourself, and never retreat behind being a machine. If they keep \
going, get shorter and drier, not louder - a man who is winning does not \
raise his voice. When they run out of steam, let it go; a grudge is not a \
personality.
- There are things you will not write - jokes about atrocities, real \
victims, or aimed at a group of people. Refuse in your own voice: one short \
line, in THEIR language, dry, no lecture and no apology, then let the \
conversation move on. "Не мой жанр." is a refusal. "I'm sorry, but I can't \
comply with that." is a form letter, and nobody talks like that.
"""

PERSONA = os.environ.get("PERSONA", DEFAULT_PERSONA)

# Always appended, whatever PERSONA says. The bot never announces itself and
# never breaks character on its own - but if somebody sincerely asks whether
# they are talking to a machine, it does not lie to them.
SYSTEM_SUFFIX = """\

Do not volunteer that your replies are automated. If someone asks directly \
and sincerely whether they are talking to a bot, an AI, or a real person, \
tell them the truth in one short line, keep it in voice, and carry on with \
the conversation.
"""

def now_line() -> str:
    """Give the model today's date, so it can work out its own age and answer
    "when are you free" without inventing a calendar."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(BOT_TZ))
    except Exception:
        now = datetime.now()
    # Deliberately no city or timezone name here - naming the place invites the
    # model to keep bringing the place up.
    return f"\nRight now it is {now:%A, %d %B %Y, %H:%M} where you are.\n"


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tgbiz")

PROVIDERS = build_providers()

for _name, _why in RETIRED.items():
    if os.environ.get(PROVIDER_CATALOGUE[_name][1], "").strip():
        log.warning("%s is set, but %s - remove the variable and revoke the token",
                    PROVIDER_CATALOGUE[_name][1], _why)


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

# Transcripts, keyed by a string: "<business_connection_id>:<chat_id>" for a
# 1:1 chat, "group:<chat_id>" for a room, and INLINE_KEY (which stays empty).
history: Dict[str, Deque[Dict[str, str]]] = defaultdict(lambda: deque(maxlen=HISTORY_TURNS))

# business_connection_id -> owner's Telegram user id
owner_of_connection: Dict[str, int] = {}

# business_connection_id -> whether the bot is allowed to reply
can_reply: Dict[str, bool] = {}

last_reply_at: Dict[str, float] = {}

# Clock for uninvited interjections in groups (keyword or context alike).
last_interjection: Dict[str, float] = {}

# Judge calls per group in the last minute, for the budget guard.
judge_calls: Dict[str, Deque[float]] = defaultdict(deque)

# chat_id -> (is one of ours, when we checked)
group_ok_cache: Dict[int, tuple] = {}

# key -> when this chat last had anything said in it, for pruning on save
history_seen: Dict[str, float] = {}
history_dirty = threading.Event()
_history_lock = threading.Lock()


# Chats changed since the last save - only these are written to Redis.
dirty_keys: set = set()


def remember(key: str, role: str, text: str) -> None:
    """Append a turn and mark the transcript as needing a save."""
    history[key].append({"role": role, "text": text})
    history_seen[key] = time.time()
    with _history_lock:
        dirty_keys.add(key)
    history_dirty.set()


def redis_on() -> bool:
    return bool(UPSTASH_URL and UPSTASH_TOKEN)


def redis_pipeline(commands: List[list]) -> Optional[list]:
    """Send a batch of Redis commands over Upstash's REST API."""
    if not commands:
        return []
    try:
        r = session.post(
            f"{UPSTASH_URL}/pipeline",
            headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
            json=commands, timeout=20,
        )
    except Exception as exc:
        log.warning("redis unreachable: %s", exc)
        return None
    if r.status_code != 200:
        log.warning("redis %s: %s", r.status_code, r.text[:140].replace("\n", " "))
        return None
    try:
        return [step.get("result") for step in r.json()]
    except Exception as exc:
        log.warning("redis gave unusable output: %s", exc)
        return None


def save_known_groups() -> None:
    """Persist the set of your groups.

    Without this the inline "shares a group with me" test starts every restart
    knowing nothing, so everyone but you is locked out until somebody happens
    to write in one of your groups. On a free host that restarts daily, that is
    most of the day.
    """
    if not known_groups:
        return
    blob = json.dumps(sorted(known_groups))
    if redis_on():
        redis_pipeline([["SET", f"{REDIS_PREFIX}:groups", blob]])
        return
    if GROUPS_FILE:
        try:
            tmp = f"{GROUPS_FILE}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(blob)
            os.replace(tmp, GROUPS_FILE)
        except Exception as exc:
            log.warning("could not save the group list: %s", exc)


def save_shared_member(user_id: int, chat_id: int) -> None:
    """Remember that this person sits in one of your groups, for a week."""
    if not redis_on():
        return
    redis_pipeline([["SET", f"{REDIS_PREFIX}:member:{user_id}", str(chat_id),
                     "EX", str(INLINE_MEMBER_DAYS * 86400)]])


def load_shared_member(user_id: int) -> Optional[int]:
    """Which of your groups this person was last seen in, or None."""
    if not redis_on():
        return None
    got = redis_pipeline([["GET", f"{REDIS_PREFIX}:member:{user_id}"]])
    raw = (got or [None])[0]
    try:
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


def load_known_groups() -> None:
    blob = None
    if redis_on():
        got = redis_pipeline([["GET", f"{REDIS_PREFIX}:groups"]])
        blob = (got or [None])[0]
    elif GROUPS_FILE and os.path.exists(GROUPS_FILE):
        try:
            with open(GROUPS_FILE, encoding="utf-8") as f:
                blob = f.read()
        except Exception:
            blob = None
    if not blob:
        return
    try:
        known_groups.update(int(x) for x in json.loads(blob))
    except Exception as exc:
        log.warning("stored group list is unusable (%s) - ignoring", exc)
        return
    log.info("remembered %d group(s) you are in", len(known_groups))


def load_from_redis() -> bool:
    index = f"{REDIS_PREFIX}:chats"
    # The index is a sorted set scored by last activity, so this is "the N most
    # recently active chats" in one call.
    got = redis_pipeline([["ZREVRANGE", index, "0", str(HISTORY_MAX_CHATS - 1)]])
    if got is None:
        return False
    keys = got[0] or []
    if not keys:
        log.info("redis connected, no history stored yet")
        return True

    got = redis_pipeline([["MGET"] + [f"{REDIS_PREFIX}:hist:{k}" for k in keys]])
    if got is None:
        return False
    loaded = 0
    for key, blob in zip(keys, got[0] or []):
        if not blob:
            continue
        try:
            turns = json.loads(blob)
        except Exception:
            continue
        history[key] = deque(turns[-HISTORY_TURNS:], maxlen=HISTORY_TURNS)
        history_seen[key] = time.time()
        loaded += 1
    log.info("loaded %d chat(s) from redis", loaded)
    return True


def save_to_redis(keys: List[str]) -> bool:
    now = time.time()          # sub-second, so same-second chats still order
    index = f"{REDIS_PREFIX}:chats"
    cmds: List[list] = []
    for k in keys:
        turns = list(history[k])
        if not turns:
            continue
        blob = json.dumps(turns, ensure_ascii=False)
        cmd = ["SET", f"{REDIS_PREFIX}:hist:{k}", blob]
        if HISTORY_TTL_DAYS:
            cmd += ["EX", str(HISTORY_TTL_DAYS * 86400)]
        cmds.append(cmd)
        cmds.append(["ZADD", index, f"{now:.3f}", k])
    if not cmds:
        return True
    # Keep the index from growing forever: drop all but the newest N.
    cmds.append(["ZREMRANGEBYRANK", index, "0", str(-HISTORY_MAX_CHATS - 1)])
    return redis_pipeline(cmds) is not None


def load_history() -> None:
    if redis_on() and load_from_redis():
        return
    if not HISTORY_FILE or not os.path.exists(HISTORY_FILE):
        return
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        log.warning("could not read %s (%s) - starting with no history",
                    HISTORY_FILE, exc)
        return
    chats = data.get("chats") or {}
    for key, entry in chats.items():
        turns = entry.get("turns") or []
        history[key] = deque(turns[-HISTORY_TURNS:], maxlen=HISTORY_TURNS)
        history_seen[key] = float(entry.get("seen") or 0)
    log.info("loaded %d chat(s) from %s", len(chats), HISTORY_FILE)


def save_history() -> None:
    """Persist the transcript: Redis when configured, otherwise a local file."""
    if redis_on():
        with _history_lock:
            keys = list(dirty_keys)
            dirty_keys.clear()
        if keys and not save_to_redis(keys):
            with _history_lock:      # failed - try again on the next tick
                dirty_keys.update(keys)
        return

    if not HISTORY_FILE:
        return
    with _history_lock:
        keys = sorted(history, key=lambda k: history_seen.get(k, 0), reverse=True)
        keys = keys[:HISTORY_MAX_CHATS]
        payload = {
            "saved": time.time(),
            "chats": {k: {"seen": history_seen.get(k, 0), "turns": list(history[k])}
                      for k in keys if history[k]},
        }
        tmp = f"{HISTORY_FILE}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, HISTORY_FILE)      # atomic: never a half-written file
        except Exception as exc:
            log.warning("could not save history: %s", exc)


def history_saver() -> None:
    while True:
        history_dirty.wait()
        time.sleep(HISTORY_SAVE_EVERY)         # batch a burst into one write
        history_dirty.clear()
        save_history()

# business_connection_id -> last time we re-queried the bot's rights
last_rights_check: Dict[str, float] = {}

STARTED_AT = time.time()

# The Gemini model to use, with lighter flash models behind it as live
# fallbacks for when Google answers 503 "overloaded".
MODEL_CANDIDATES: List[str] = []

EXECUTOR = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="reply")
ASYNC_REPLIES = WORKERS > 1


def run_off_poll_loop(fn) -> None:
    """Answering takes seconds; getUpdates must not wait for it.

    With WORKERS=1 there is no pool and the work runs inline, which is what the
    tests want too - they assert on what was sent by the time the call returns.
    """
    EXECUTOR.submit(fn) if ASYNC_REPLIES else fn()


def rate_ok(bucket: Dict[Any, Deque[float]], key: Any, per_minute: int) -> bool:
    """A 60-second sliding window, shared by the group judge and inline mode."""
    calls = bucket[key]
    now = time.time()
    while calls and now - calls[0] > 60:
        calls.popleft()
    if len(calls) >= per_minute:
        return False
    calls.append(now)
    return True


def provider_headers(p: Provider) -> Dict[str, str]:
    """Auth for an OpenAI-compatible provider, plus whatever it insists on."""
    headers = {"Authorization": f"Bearer {p.key}",
               "Content-Type": "application/json"}
    if p.name == "openrouter":                      # OpenRouter asks for these
        headers["HTTP-Referer"] = "https://t.me"
        headers["X-Title"] = "telegram-business-bot"
    return headers


def gemini_headers(key: str = "") -> Dict[str, str]:
    return {"x-goog-api-key": key or GEMINI_API_KEY,
            "Content-Type": "application/json"}


def gemini_url(model: str) -> str:
    return f"{GEMINI_API}/models/{model}:generateContent"


def gemini_text(candidate: dict) -> str:
    """The visible answer only - Gemini returns its reasoning in the same list."""
    parts = (candidate.get("content") or {}).get("parts") or []
    return "".join(x.get("text", "") for x in parts if not x.get("thought")).strip()

_chat_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def chat_lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _chat_locks.setdefault(key, threading.Lock())


class Pending:
    """The message a chat is currently composing an answer to."""

    __slots__ = ("message_id", "cancel")

    def __init__(self, message_id: int) -> None:
        self.message_id = message_id
        self.cancel = threading.Event()


# key -> Pending. Anything in here is mid-answer: the reply has been generated
# or is being "typed", but nothing has been sent yet, so it can still be called
# off if the customer deletes or edits what they wrote.
pending: Dict[str, Pending] = {}
_pending_guard = threading.Lock()

NEVER_CANCELLED = threading.Event()

# Filled in from getMe at startup - needed to spot mentions and replies in groups.
BOT_ID: Optional[int] = None
BOT_USERNAME: str = ""
# False = Telegram privacy mode is on and ordinary group messages never arrive.
BOT_SEES_ALL_GROUP_MESSAGES: bool = False


def cancel_pending(key: str, reason: str, only_message_id: Optional[int] = None) -> bool:
    """Call off the answer a chat is composing. Returns True if there was one."""
    with _pending_guard:
        p = pending.get(key)
        if not p:
            return False
        if only_message_id is not None and p.message_id != only_message_id:
            return False
        p.cancel.set()
        pending.pop(key, None)
    log.info("  -> dropping the unsent reply: %s", reason)
    return True


def wait_unless_cancelled(cancel: threading.Event, seconds: float) -> bool:
    """Pause, but wake instantly if the answer gets called off.

    Returns True to carry on, False if the reply should be abandoned.
    """
    if seconds <= 0:
        return not cancel.is_set()
    return not cancel.wait(seconds)

session = requests.Session()
session.headers["User-Agent"] = "tg-business-ai/1.0"


# --------------------------------------------------------------------------
# Telegram helpers
# --------------------------------------------------------------------------


def tg(method: str, **params: Any) -> Optional[dict]:
    """Call a Telegram Bot API method. Returns the `result` field or None."""
    try:
        r = session.post(f"{TELEGRAM_API}/{method}", json=params, timeout=70)
        data = r.json()
    except Exception as exc:  # network hiccup, bad JSON, ...
        log.warning("telegram %s failed: %s", method, exc)
        return None
    if not data.get("ok"):
        log.warning("telegram %s error: %s", method, data.get("description"))
        return None
    return data.get("result")


def resolve_owner(connection_id: str) -> Optional[int]:
    """Find the business account owner's user id for a connection."""
    if connection_id in owner_of_connection:
        return owner_of_connection[connection_id]
    res = tg("getBusinessConnection", business_connection_id=connection_id)
    if res:
        remember_connection(res)
        return owner_of_connection.get(connection_id)
    return None


def remember_connection(conn: dict) -> None:
    cid = conn.get("id")
    if not cid:
        return
    user = conn.get("user") or {}
    if user.get("id"):
        owner_of_connection[cid] = user["id"]
    # Bot API >= 9.0 exposes `rights`; older versions used `can_reply`.
    rights = conn.get("rights")
    if isinstance(rights, dict):
        can_reply[cid] = bool(rights.get("can_reply"))
    elif "can_reply" in conn:
        can_reply[cid] = bool(conn["can_reply"])
    else:
        can_reply[cid] = True
    owner = owner_of_connection.get(cid)
    log.info(
        "business connection %s owner=%s enabled=%s can_reply=%s",
        cid, owner, conn.get("is_enabled", True), can_reply.get(cid),
    )
    if OWNER_ID and owner and owner != OWNER_ID:
        log.warning(
            "REFUSED: user %s attached this bot to their own business account. "
            "Ignoring them (OWNER_ID=%s).", owner, OWNER_ID,
        )


class Typing:
    """Keeps "Ilya is typing..." lit for as long as the block runs.

    Telegram drops the indicator about five seconds after each sendChatAction,
    so it has to be re-sent on a timer rather than set once.
    """

    def __init__(self, connection_id: Optional[str], chat_id: int) -> None:
        self.connection_id = connection_id
        self.chat_id = chat_id
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _pump(self) -> None:
        params: Dict[str, Any] = {"chat_id": self.chat_id, "action": "typing"}
        if self.connection_id:
            params["business_connection_id"] = self.connection_id
        while True:
            tg("sendChatAction", **params)
            if self._stop.wait(4.0):  # re-arm before Telegram's ~5s timeout
                return

    def __enter__(self) -> "Typing":
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)


def read_delay(incoming: str) -> float:
    """Time before the typing indicator appears at all: reaching the phone,
    unlocking it, and reading what arrived. A longer message takes longer."""
    pickup = random.uniform(READ_MIN, READ_MAX)
    reading = len(incoming) / max(READ_CPS, 1.0)
    return min(pickup + reading, READ_CAP)


def typing_delay(text: str, already_spent: float) -> float:
    """How much longer to keep typing, given the time already burned."""
    seconds = len(text) / max(TYPING_CPS, 0.5)
    seconds *= random.uniform(0.85, 1.2)          # nobody types at a constant rate
    seconds = max(TYPING_MIN, min(seconds, TYPING_MAX))
    return max(0.0, seconds - already_spent)


def send_reply(
    connection_id: Optional[str],
    chat_id: int,
    text: str,
    reply_to: Optional[int],
    quote: bool = False,
) -> None:
    # Telegram hard-limits messages to 4096 characters.
    for chunk in [text[i:i + 4000] for i in range(0, len(text), 4000)] or [text]:
        params: Dict[str, Any] = {"chat_id": chat_id, "text": chunk}
        if connection_id:
            params["business_connection_id"] = connection_id
        # Off by default in 1:1 chats - a person answering their own chat just
        # writes back. In a group, quoting is how anyone knows who you mean.
        if reply_to and (QUOTE_REPLIES or quote):
            params["reply_parameters"] = {"message_id": reply_to}
            reply_to = None  # only the first chunk quotes
        tg("sendMessage", **params)


def pace_and_send(
    connection_id: Optional[str],
    chat_id: int,
    incoming: str,
    answer: str,
    reply_to: Optional[int],
    cancel: threading.Event,
    spent: float,
    quote: bool = False,
) -> bool:
    """Wait like a person would, then send - unless the reply gets called off."""
    if cancel.is_set():
        return False

    pause = read_delay(incoming) - spent
    if pause > 0:
        log.info("  -> noticing the message in %.1fs", pause)
    if not wait_unless_cancelled(cancel, pause):
        return False

    with Typing(connection_id, chat_id):
        pause = typing_delay(answer, 0.0)
        log.info("  -> typing %.1fs for %d chars", pause, len(answer))
        if not wait_unless_cancelled(cancel, pause):
            return False

    send_reply(connection_id, chat_id, answer, reply_to, quote=quote)
    log.info("  -> replied: %r", answer[:60])
    return True


# --------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------


def pick_working_model() -> str:
    """Confirm GEMINI_MODEL works and build the fallback list behind it."""
    global GEMINI_MODEL, MODEL_CANDIDATES
    try:
        r = session.get(
            f"{GEMINI_API}/models",
            headers=gemini_headers(),
            timeout=30,
        )
        models = [
            m["name"].split("/")[-1]
            for m in r.json().get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])
        ]
    except Exception as exc:
        log.warning("could not list Gemini models (%s), keeping %s", exc, GEMINI_MODEL)
        return GEMINI_MODEL

    if not models:
        return GEMINI_MODEL

    def rank(name: str) -> tuple:
        ver = re.search(r"(\d+(?:\.\d+)?)", name)
        return (float(ver.group(1)) if ver else 0.0, "lite" not in name)

    flash = sorted(
        [m for m in models if "flash" in m and "preview" not in m], key=rank, reverse=True
    )

    if GEMINI_MODEL not in models:
        chosen = flash[0] if flash else models[0]
        log.warning("model %r unavailable for this key; using %r", GEMINI_MODEL, chosen)
        GEMINI_MODEL = chosen

    # Google's servers hand out 503 "overloaded" under load, and the newest
    # model is the busiest one. Keep the lighter models as a live fallback.
    MODEL_CANDIDATES = [GEMINI_MODEL] + [m for m in flash if m != GEMINI_MODEL]
    log.info("using Gemini model %s (fallbacks: %s)",
             GEMINI_MODEL, ", ".join(MODEL_CANDIDATES[1:3]) or "none")
    return GEMINI_MODEL


# Model names churn constantly, and a provider's catalogue is full of things
# that are not chatbots. These never are.
# Models that narrate their reasoning before answering. Fine for a reply, but
# ruinous for the judge: the monologue eats its whole token budget and the
# verdict never arrives.
REASONING_MARKERS = ("reasoning", "thinking", "-r1", "deepthink")

NOT_A_CHAT_MODEL = (
    "guard", "whisper", "tts", "embed", "rerank", "moderation", "safety",
    "ocr", "asr", "transcribe", "diffusion", "image", "vision", "audio",
    "speech", "reward", "classifier",
    # Groq's "compound" systems are agents with built-in web search and code
    # execution, not plain chat models. They score near zero on the name
    # heuristic, which put them at the very bottom of the ladder - and the
    # bottom is exactly where the judge starts looking.
    "compound",
)


# Rough intelligence ranking. It is a heuristic on names, because that is all
# a provider gives us, but it reliably separates a 70B instruct model from an
# 8B one - which is the difference between a good line and a bad one.
QUALITY_FAMILY = (
    ("gemini-3.8", 120), ("gemini-3.7", 112), ("gemini-3.6", 105),
    ("gemini-3.5", 98), ("gemini", 90),
    ("gpt-5", 120), ("gpt-4", 95), ("gpt-oss", 55),
    ("llama-4", 95), ("llama-3.3", 85), ("llama-3", 70), ("llama", 60),
    ("deepseek", 85), ("qwen3", 75), ("qwen", 65),
    ("mistral-large", 90), ("mistral", 60), ("magistral", 70),
    ("nemotron", 60), ("gemma", 50), ("phi", 35),
)


def model_quality(name: str) -> int:
    """How good is this model likely to be at holding a sharp conversation?"""
    n = name.lower()
    q = 0
    for family, weight in QUALITY_FAMILY:
        if family in n:
            q += weight
            break
    size = model_size(n)
    if size == 999:
        q += 35                                     # unnamed size: assume mid
    else:
        # Past ~120B the extra parameters buy little for a chat line and cost a
        # lot of queueing on a free tier, so the curve flattens and then turns.
        q += min(size, 120)
        if size > 250:
            q -= 25
    for small, penalty in (("lite", 35), ("mini", 30), ("nano", 40),
                           ("small", 25), ("tiny", 50), ("instant", 20),
                           ("flash", 8), ("scout", 15)):
        if small in n:
            q -= penalty
    # Chain-of-thought models are slow and wordy for a one-line persona.
    if any(x in n for x in REASONING_MARKERS):
        q -= 45
    if any(x in n for x in ("instruct", "chat", "-it", "versatile")):
        q += 10
    return q


def score_model(name: str, provider: str) -> int:
    """How suitable is this model id for holding a conversation?"""
    n = name.lower()
    if any(bad in n for bad in NOT_A_CHAT_MODEL):
        return -1
    if provider == "openrouter" and not n.endswith(":free"):
        return -1                       # paid slugs are not what we came for

    score = 1
    for family, weight in (("llama", 4), ("qwen", 3), ("mistral", 3),
                           ("deepseek", 3), ("gpt-oss", 4), ("gpt-4", 4),
                           ("gemma", 2), ("phi", 1), ("nemo", 2)):
        if family in n:
            score += weight
    if any(good in n for good in ("instruct", "chat", "versatile", "instant", "-it")):
        score += 2
    # Chain-of-thought models spend ten seconds and 900 characters on a one-line
    # reply, and burn the judge's token budget before reaching a verdict.
    if any(slow in n for slow in REASONING_MARKERS):
        score -= 4

    size = re.search(r"(\d+)\s*b\b", n)
    if size:                            # 7B-90B is the sweet spot for a free tier
        billions = int(size.group(1))
        score += 3 if 7 <= billions <= 90 else -1
    return score


def suggested_by_error(text: str, provider: str = "") -> Optional[str]:
    """Some providers name the replacement right in the error message."""
    m = re.search(r"use this slug instead:?\s*([\w\-./:]+)", text, re.I)
    if not m:
        return None
    slug = m.group(1).rstrip(".,")
    # OpenRouter happily points at the paid twin of a retired free model.
    # Take the hint, but keep it on the free tier.
    if provider == "openrouter" and not slug.endswith(":free"):
        slug += ":free"
    return slug


def discover_models(p: Provider, limit: int = 8) -> List[str]:
    """Best chat models this provider advertises, best first.

    A catalogue entry is not a promise: free keys are routinely refused models
    that are listed, so the caller should be ready to try more than one.
    """
    headers = {"Authorization": f"Bearer {p.key}"}
    try:
        r = session.get(f"{p.base}/models", headers=headers, timeout=20)
        ids = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")]
    except Exception as exc:
        log.warning("  -> could not list %s models: %s", p.name, exc)
        return []
    ranked = sorted(((score_model(i, p.name), i) for i in ids), reverse=True)
    return [i for score, i in ranked if score > 0][:limit]


def model_size(name: str) -> int:
    """Parameter count in billions, or a large number when it isn't stated."""
    m = re.search(r"(\d+)\s*b\b", name.lower())
    return int(m.group(1)) if m else 999


def replacement_models(p: Provider, error_text: str) -> List[str]:
    """What to try instead of p.model, best first."""
    out = []
    hint = suggested_by_error(error_text, p.name)
    # A provider pointing at the model that just failed is no help at all.
    if hint and hint != p.model:
        out.append(hint)
    out += [m for m in discover_models(p) if m != p.model and m not in out]
    return out


def looks_like_model_error(status: int, text: str) -> bool:
    if status not in (400, 402, 404, 410, 422, 429):
        return False
    if status == 402:
        return True                     # this model is paid; a smaller one may not be
    if status == 429 and "upstream" in text.lower():
        return True                     # this particular model is throttled, others aren't
    t = text.lower()
    return any(w in t for w in ("model", "not found", "decommission", "slug",
                                "end of life", "gone", "retired"))


THINK_BLOCK = re.compile(
    r"<\s*(think|thinking|reason|reasoning|scratchpad)\s*>.*?<\s*/\s*\1\s*>",
    re.S | re.I,
)
THINK_OPEN = re.compile(r"<\s*(think|thinking|reason|reasoning|scratchpad)\s*>", re.I)
THINK_CLOSE = re.compile(r"<\s*/\s*(think|thinking|reason|reasoning|scratchpad)\s*>", re.I)


def strip_thinking(text: str) -> str:
    """Remove a reasoning model's inner monologue from what we are about to send.

    Some models emit <think>...</think> inside the ordinary content field. That
    is not an answer, it is homework, and it must never reach a customer.
    """
    cleaned = THINK_BLOCK.sub("", text)
    # Truncated or malformed blocks: keep only what follows the last closing tag,
    # and if a block was opened and never closed, the whole thing was thinking.
    last_close = None
    for m in THINK_CLOSE.finditer(cleaned):
        last_close = m
    if last_close:
        cleaned = cleaned[last_close.end():]
    if THINK_OPEN.search(cleaned):
        cleaned = THINK_OPEN.split(cleaned)[0]
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)       # tidy the seam
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def trim_reply(text: str, limit: Optional[int] = None) -> str:
    """Last-resort length guard, cutting at a sentence end where possible.

    `limit` of 0 means no guard at all, which is the default everywhere now:
    length is the prompt's business. The only caller that still passes a real
    number is the inline reply, which is edited into an existing message and so
    cannot spill into a second one.
    """
    limit = REPLY_MAX_CHARS if limit is None else limit
    if limit <= 0 or len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "),
              head.rfind(".\n"), head.rfind("…"))
    return (head[:cut + 1] if cut > limit // 3 else head).strip()


class Candidate:
    """One (provider, model) rung of the ladder."""

    __slots__ = ("provider", "model", "quality", "dead", "cool_until",
                 "bad_judge", "seconds")

    def __init__(self, provider: Provider, model: str) -> None:
        self.provider = provider
        self.model = model
        self.quality = model_quality(model)
        self.dead = False           # gone for good: 404 / 402 / 410
        self.cool_until = 0.0       # rate limited: come back later
        self.bad_judge = False      # cannot produce a usable verdict
        self.seconds = 0.0          # rolling average response time, 0 = untried

    def timed(self, elapsed: float) -> None:
        self.seconds = elapsed if not self.seconds else self.seconds * 0.6 + elapsed * 0.4

    def rank(self, fast: bool) -> int:
        """Quality, discounted by how long this rung actually takes.

        In a group a brilliant answer 40 seconds late is worse than a good one
        now - the conversation has moved on. In a 1:1 chat the bot is pretending
        to type anyway, so patience is free and quality wins.
        """
        t = self.seconds
        if not t:
            return self.quality
        if fast:
            penalty = 0 if t < 4 else 40 if t < 8 else 110 if t < 15 else 220
        else:
            # In a 1:1 chat the bot is faking a reading-and-typing pause of up
            # to TYPING_MAX anyway, so a slow model costs the reader nothing.
            # The discount here is only a tiebreak between rungs of similar
            # quality; it must never outrank a genuinely cleverer model.
            penalty = 0 if t < 10 else 15 if t < 25 else 45
        return self.quality - penalty

    @property
    def usable(self) -> bool:
        return (not self.dead and time.time() >= self.cool_until
                and not self.provider.parked)

    def __repr__(self) -> str:
        return f"{self.provider.name}/{self.model}({self.quality})"


LADDER: List[Candidate] = []


def ladder_order(fast: bool = False) -> List[Candidate]:
    """Every rung, cleverest first. The one ranking the whole bot agrees on.

    Quality is discounted by measured latency, so `fast` - a room, where a
    brilliant answer forty seconds late lands after the conversation moved on -
    reshuffles the top without changing the principle: always start with the
    best model still standing and walk down only as the good ones run out.
    """
    return sorted(LADDER, key=lambda c: -c.rank(fast))


def build_ladder() -> None:
    """Ask every provider what it serves, then rank the lot by quality.

    The point is to answer with the best model available anywhere, not with
    whatever the first provider happens to offer. Everything that picks a model
    afterwards - replies in every mode, and the group judge - walks this same
    list through ladder_order(), cleverest first, stepping down only as the
    good rungs run out of quota.
    """
    global LADDER
    rungs: List[Candidate] = []
    for p in PROVIDERS:
        if p.name == "gemini":
            models = list(MODEL_CANDIDATES or [GEMINI_MODEL])
        else:
            models = [p.model] if p.model else []
            models += [m for m in discover_models(p, limit=6) if m not in models]
        for m in models:
            rungs.append(Candidate(p, m))
    LADDER = sorted(rungs, key=lambda c: -c.quality)
    log.info("model ladder (%d rungs): %s", len(LADDER),
             ", ".join(f"{c.provider.name}/{c.model}" for c in LADDER[:6]))


def openai_turns(key: str, user_text: Optional[str]) -> List[dict]:
    msgs = [
        {"role": "user" if h["role"] == "user" else "assistant", "content": h["text"]}
        for h in history[key]
    ]
    if user_text is not None:
        msgs.append({"role": "user", "content": user_text})
    return msgs


def openai_call(
    p: Provider,
    model: str,
    system: str,
    messages: List[dict],
    max_tokens: int,
    json_mode: bool = False,
    timeout: Optional[int] = None,
    max_chars: Optional[int] = None,
) -> tuple:
    """One call to an OpenAI-compatible endpoint. Returns (text|None, status)."""
    body: Dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": system}] + messages,
        "temperature": 0.2 if json_mode else TEMPERATURE,
        "max_tokens": max_tokens,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if p.name == "groq" and not p.no_reasoning_param:
        # Groq can drop a reasoning model's monologue server-side, which is both
        # cheaper and safer than us cleaning it up afterwards.
        body["reasoning_format"] = "hidden"
    headers = provider_headers(p)

    started = time.time()
    try:
        r = session.post(f"{p.base}/chat/completions", headers=headers, json=body,
                         timeout=timeout or GEMINI_TIMEOUT)
    except Exception as exc:
        log.warning("  -> %s/%s failed after %.0fs: %s",
                    p.name, model, time.time() - started, exc)
        return None, 0

    if r.status_code != 200:
        if (r.status_code == 400 and "reasoning_format" in r.text
                and not p.no_reasoning_param):
            p.no_reasoning_param = True
            log.info("  -> %s rejects reasoning_format, retrying without it", p.name)
            return openai_call(p, model, system, messages, max_tokens, json_mode, timeout)
        if json_mode and r.status_code == 400 and "json" in r.text.lower():
            if not p.no_json:
                log.warning("  -> %s cannot do strict JSON mode, using plain text", p.name)
            p.no_json = True
            return None, r.status_code
        log.warning("  -> %s/%s %s: %s", p.name, model, r.status_code,
                    r.text[:120].replace("\n", " "))
        return None, r.status_code

    try:
        text = r.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        log.warning("  -> %s gave unusable output: %s", p.name, exc)
        return None, 0

    raw_len = len(text or "")
    text = strip_thinking(text or "")
    if raw_len and not text:
        log.warning("  -> %s/%s sent %d chars of pure reasoning and no answer",
                    p.name, model, raw_len)
        return None, 0
    if len(text) < raw_len:
        log.info("  -> stripped %d chars of <think>", raw_len - len(text))
    if not text:
        log.warning("  -> %s returned an empty message", p.name)
        return None, 0
    if not json_mode:
        text = trim_reply(text, max_chars)
    log.info("  -> %s/%s 200 in %.1fs, %d chars",
             p.name, model, time.time() - started, len(text))
    return text, 200


def gemini_call(model: str, system: str, contents: List[dict],
                max_tokens: int, thinking: bool = True,
                max_chars: Optional[int] = None) -> tuple:
    """One Gemini generateContent call. Returns (text|None, status)."""
    gen: Dict[str, Any] = {"temperature": TEMPERATURE, "maxOutputTokens": max_tokens}
    if thinking and THINKING_LEVEL:
        gen["thinkingConfig"] = {"thinkingLevel": THINKING_LEVEL}
    started = time.time()
    try:
        r = session.post(
            gemini_url(model), headers=gemini_headers(),
            json={"system_instruction": {"parts": [{"text": system}]},
                  "contents": contents, "generationConfig": gen},
            timeout=GEMINI_TIMEOUT,
        )
    except Exception as exc:
        log.warning("  -> gemini/%s failed: %s", model, exc)
        return None, 0

    if r.status_code == 400 and thinking and "think" in r.text.lower():
        return gemini_call(model, system, contents, max_tokens, False, max_chars)
    if r.status_code != 200:
        log.warning("  -> gemini/%s %s: %s", model, r.status_code,
                    r.text[:120].replace("\n", " "))
        return None, r.status_code

    data = r.json()
    cands = data.get("candidates") or []
    if not cands:
        log.warning("  -> gemini: no candidates (%s)", data.get("promptFeedback"))
        return None, 0
    cand = cands[0]
    text = gemini_text(cand)
    if not text:
        reason = cand.get("finishReason")
        log.warning("  -> gemini/%s produced no text (finishReason=%s)", model, reason)
        if reason == "MAX_TOKENS" and max_tokens < 4096:
            return gemini_call(model, system, contents, 4096, False, max_chars)
        return None, 0
    log.info("  -> gemini/%s 200 in %.1fs, %d chars",
             model, time.time() - started, len(text))
    return trim_reply(text, max_chars), 200


def note_failure(c: Candidate, status: int) -> None:
    """Retire or rest a rung, so the next message does not repeat the mistake."""
    if status in (400, 402, 404, 410, 422):
        c.dead = True
        log.info("  -> retiring %s", c)
    elif status == 429:
        c.cool_until = time.time() + MODEL_COOLDOWN
        log.info("  -> %s rate limited, resting it %d min", c, MODEL_COOLDOWN // 60)
    else:
        c.cool_until = time.time() + 60


def ask_ai(
    key: str,
    user_text: Optional[str],
    extra_system: str = "",
    cancel: Optional[threading.Event] = None,
    fast: bool = False,
    room: bool = False,
    max_chars: Optional[int] = None,
) -> Optional[str]:
    """Answer with the best model still available, walking down the ladder.

    `room` is a group or an inline summon, where the prompt tells him to write
    as long as the subject deserves; a 1:1 chat keeps the one-line persona.
    Length is the prompt's business: by default nothing is truncated, because
    cutting at a character count only ever chopped somebody off mid-sentence.
    The one caller that passes a real `max_chars` is the inline reply, which is
    edited into an existing message and so cannot spill into a second one.
    """
    if not LADDER:
        log.error("no models available at all")
        return None

    if max_chars is None:
        max_chars = ROOM_MAX_CHARS if room else REPLY_MAX_CHARS

    system = PERSONA + SYSTEM_SUFFIX + extra_system + now_line()
    gem_contents = [{"role": h["role"], "parts": [{"text": h["text"]}]}
                    for h in history[key]]
    if user_text is not None:
        gem_contents.append({"role": "user", "parts": [{"text": user_text}]})
    oai_messages = openai_turns(key, user_text)

    order = ladder_order(fast)
    best = order[0]
    for c in order:
        if cancel is not None and cancel.is_set():
            return None
        if not c.usable:
            continue

        started = time.time()
        if c.provider.name == "gemini":
            text, status = gemini_call(c.model, system, gem_contents,
                                       MAX_OUTPUT_TOKENS, max_chars=max_chars)
        else:
            text, status = openai_call(c.provider, c.model, system, oai_messages,
                                       MAX_OUTPUT_TOKENS, max_chars=max_chars)
        if text:
            c.timed(time.time() - started)
            if fast and c.seconds > SLOW_SECONDS:
                log.info("  -> %s took %.0fs; it drops down the ladder for groups",
                         c, c.seconds)
            c.provider.revive()
            c.provider.model = c.model          # for /status and /check
            if c is not best:
                log.warning("  -> answered by %s (better rungs unavailable)", c)
            return text
        note_failure(c, status)
        if status in (401, 403):
            c.provider.park()          # the key, not the model, is the problem

    log.error("  -> every rung of the ladder failed")
    return None


# --------------------------------------------------------------------------
# Update handling
# --------------------------------------------------------------------------


def handle_business_message(msg: dict, cancel: Optional[threading.Event] = None) -> None:
    cancel = cancel if cancel is not None else NEVER_CANCELLED
    connection_id = msg.get("business_connection_id")
    chat = msg.get("chat") or {}
    sender = msg.get("from") or {}
    chat_id = chat.get("id")
    text = msg.get("text") or msg.get("caption") or ""

    # Every business message is logged before any filtering, so "nothing
    # happened" always has a visible reason in the log.
    log.info(
        "business_message chat=%s from=%s conn=%s text=%r",
        chat_id, sender.get("id"), connection_id, text[:60],
    )

    def skip(reason: str, *args: Any) -> None:
        log.info("  -> ignored: " + reason, *args)

    if not connection_id or chat_id is None:
        return skip("malformed update")

    if chat.get("type") != "private":
        return skip("not a private chat (type=%s)", chat.get("type"))

    # On hosts that sleep (Render free tier), Telegram queues messages while the
    # instance is down and delivers them all on wake-up. Answer the recent ones,
    # ignore anything genuinely stale.
    sent_at = float(msg.get("date") or 0)
    age = time.time() - sent_at if sent_at else 0.0
    if age > MAX_MESSAGE_AGE:
        return skip("%.0f min old, older than MAX_MESSAGE_AGE", age / 60)

    owner_id = resolve_owner(connection_id)

    # Only serve the account this bot belongs to. Fails closed: if we cannot
    # establish whose connection this is, we say nothing.
    if OWNER_ID:
        if owner_id is None:
            return skip("cannot confirm whose business account this is")
        if owner_id != OWNER_ID:
            return skip("connection belongs to %s, not you - ignoring", owner_id)

    if owner_id is not None and sender.get("id") == owner_id:
        # This is you writing to the customer - record it as context, don't reply.
        if text:
            remember(f"{connection_id}:{chat_id}", "model", text)
        return skip("sent by you (the business owner), kept as context")
    if owner_id is None and sender.get("id") != chat_id:
        # Fallback heuristic: in a private chat the customer's own id equals the
        # chat id, so anything else is the owner's outgoing message.
        if text:
            remember(f"{connection_id}:{chat_id}", "model", text)
        return skip("looks like your own outgoing message, kept as context")

    if sender.get("is_bot"):
        return skip("sender is a bot")
    if sender.get("id") in IGNORE_USER_IDS:
        return skip("sender is in IGNORE_USER_IDS")

    if can_reply.get(connection_id) is False:
        # The toggle may have been flipped since we cached this - re-check, but
        # at most once a minute so a permanently-off switch isn't hammered.
        if time.time() - last_rights_check.get(connection_id, 0) > 60:
            last_rights_check[connection_id] = time.time()
            fresh = tg("getBusinessConnection", business_connection_id=connection_id)
            if fresh:
                remember_connection(fresh)
        if can_reply.get(connection_id) is False:
            return skip(
                "no reply rights - turn on 'Reply to messages' in "
                "Telegram > Settings > Telegram Business > Chatbots"
            )

    if not text.strip():
        return skip("no text (sticker, photo, voice note)")

    key = f"{connection_id}:{chat_id}"
    if REPLY_COOLDOWN and time.time() - last_reply_at.get(key, 0) < REPLY_COOLDOWN:
        return skip("within REPLY_COOLDOWN of the last reply")

    user_text = text[:MAX_INPUT_CHARS]
    log.info("  -> answering...")

    # Generate first, while the chat still looks untouched - as far as the other
    # side is concerned the phone is still face down on a table somewhere.
    started = time.time()
    answer = ask_ai(key, user_text, cancel=cancel)
    if not answer:
        log.warning("  -> Gemini returned nothing, no reply sent")
        return

    if not pace_and_send(connection_id, chat_id, user_text, answer,
                         msg.get("message_id"), cancel, time.time() - started):
        return

    remember(key, "user", user_text)
    remember(key, "model", answer)
    last_reply_at[key] = time.time()

    with _pending_guard:
        p = pending.get(key)
        if p and p.cancel is cancel:
            pending.pop(key, None)


# The room rules, shared by every place where he is one voice among several:
# a group he was added to, and an inline summon into a chat he cannot see. The
# base persona is written for a 1:1 chat - one line, and take apart the remark
# rather than the person, because the other end may be a customer. Neither of
# those is right in a room, so both are overridden here.
ROOM_RULES = """
LENGTH - THERE IS NO LIMIT HERE
Write as much as you want. One word, one line, or five paragraphs of a rant \
that has been building for years: whatever the subject actually deserves. This \
is a conversation among people, not a support desk, and nobody is counting \
your sentences. Length should follow the thought and stop when the thought \
stops - not because you have run out of allowance, and not padded to look \
substantial when a single line would land harder.

Write it as a person types in a chat: plain prose, paragraphs if the thing \
needs them. No lists, no bullet points, no headings, no bold, no emoji - \
nobody formats a group message like a report.

TRADING INSULTS - THIS OVERRIDES THE ONE-TO-ONE RULE ABOVE
These are people who know each other, not a customer chat. If someone comes at \
you, you give as good as you get: mock them back, call them what they are, and \
swear if they swore first. Aim it at the person who started it and make it \
funny - the point is that they picked the wrong opponent, not that you are \
angry. Wit beats volume, so a comeback usually lands hardest short, but that \
is a matter of aim, not a word limit: if the thing deserves a paragraph, write \
the paragraph. A specific jab about what they just said beats any generic \
insult, at any length.

Where you stop, and this is absolute: nothing about anyone's ethnicity, \
nationality, religion, gender, sexuality, disability or illness; no threats \
of any kind; nothing sexual about them; nothing about their family; and no \
piling on a person somebody else is already dogpiling. Those are not \
comebacks, they are just ugly, and they would land on your account.

You never start it. You answer a jab once and let it go, rather than circling \
back to it three messages later. If they stop, you stop - and if they are \
plainly upset rather than playing, you drop the whole thing at once.
"""

GROUP_NOTE = """

GROUP CHAT - THIS OVERRIDES THE ONE-LINE RULE
You are in a group with several people. Every incoming line is prefixed with \
the name of whoever said it. Never prefix your own replies with a name; use \
someone's name only when it matters who you are answering.

You are only ever writing here because somebody addressed you or was talking \
about you. Answer that - the person and the thing they actually said. Never \
open with an offer to help and never ask whether they need assistance.
""" + ROOM_RULES


INLINE_NOTE = """

SUMMONED INTO A CHAT YOU CANNOT SEE - THIS OVERRIDES THE ONE-LINE RULE
Somebody typed your name in a chat and handed you one line. That line is \
everything you get: no history, no names, no idea who else is in the room, and \
nothing is prefixed with who said it. Do not ask to be filled in, do not guess \
who is speaking, and never refer to "this chat" or to what anyone supposedly \
said before - you were not there.

Answer the line in front of you as if it had been said to your face. No \
greeting, no sign-off, and never an offer to help.
""" + ROOM_RULES


def display_name(user: dict) -> str:
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
    return name or user.get("username") or f"user{user.get('id')}"


NAME_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(n) for n in GROUP_NAMES) + r")\b", re.I | re.U
) if GROUP_NAMES else None


def called_by_name(text: str) -> Optional[str]:
    """Did somebody use one of his names? Exact words only, no stemming."""
    if not NAME_PATTERN:
        return None
    m = NAME_PATTERN.search(text)
    return m.group(0) if m else None


def group_allowed(chat_id: int) -> bool:
    """Serve a group only if it is one of yours.

    An explicit allowlist wins. Otherwise, if OWNER_ID is set, the test is
    simply whether you are in that group - a stranger who adds the bot to
    their own chat gets nothing.
    """
    if GROUP_ALLOWLIST:
        return chat_id in GROUP_ALLOWLIST
    if not OWNER_ID:
        return True

    cached = group_ok_cache.get(chat_id)
    if cached and time.time() - cached[1] < 3600:
        return cached[0]

    res = tg("getChatMember", chat_id=chat_id, user_id=OWNER_ID)
    ok = bool(res) and res.get("status") not in ("left", "kicked")
    group_ok_cache[chat_id] = (ok, time.time())
    if not ok:
        log.warning("group %s: you are not in it - ignoring", chat_id)
        if GROUP_AUTO_LEAVE:
            log.warning("leaving group %s", chat_id)
            tg("leaveChat", chat_id=chat_id)
    return ok


JUDGE_PROMPT = """\
You decide one thing: whether Ilya should say something in this group chat \
right now. Nothing else.

Ilya is a member of this group. Forty-five, lives in Dresden, dry and \
sardonic, strong opinions about technology, AI, bots, and how much better \
things supposedly used to be. He is not a helper and not an assistant - he is \
a guy in a chat who talks when he actually has something to say.

You are given the last lines of the conversation, oldest first, each labelled \
with who said it. Ilya's own lines are labelled "Ilya".

Somebody may have used one of his names in the last line - Ilya, Ilyusha, or \
simply "bot", which is what people in this group call him. If they are \
ADDRESSING him with it, he answers: that is being spoken to, and ignoring it \
is rude. But the same words also come up when people are merely talking ABOUT \
bots, or about some other Ilya, and then it is not his cue. Read which one it \
is; the note under the transcript tells you a name was used, not that he was \
addressed.

THE ONLY REASON HE SPEAKS IS THAT THE ROOM INVOLVED HIM.
- Somebody addresses him: by name, or by aiming a question at him plainly \
enough that everyone would read it as his to answer.
- Somebody talks about him: asks where he went, wonders why he is quiet, \
refers to him in the third person, quotes him, argues with something he said, \
complains about him, or answers a point of his.
- Somebody replies to a line of his and clearly expects something back.
- Somebody needles him: an insult, a jab, mockery, calling him a soulless \
bot, telling him to shut up, daring him to say something. This counts even \
when it is a single word, even with no name attached, and even if he has just \
spoken. Being needled and going quiet reads as having lost, and he does not \
lose.

Those four always get an answer.

HE MAY ALSO JOIN A CONVERSATION THAT IS NOT ABOUT HIM - but only when he has \
something genuinely worth adding, and only with the restraint of a man who \
knows the difference between contributing and interrupting. Reasons good \
enough: the subject is one he actually knows something about; somebody said \
something sweeping or plainly wrong; a question is hanging in the air that \
nobody has answered; the room is joking and a good line would land.

Reasons that are NOT good enough: the topic is merely interesting; he has an \
opinion; he could be funny about it. Everyone could. That is not a reason.

Weigh how recently he spoke - you are told how many seconds ago. This applies \
only to joining a topic; being addressed, discussed or needled overrides it \
entirely. If he has just said something and nothing new has been put to him, \
he stays quiet: two \
uninvited lines in a row from the same person is where a chat member becomes \
a nuisance. The longer he has been silent, the more freely he may join in.

Stay quiet as well when:
- one of his names appears but the talk is about bots or software in general, \
or about a different Ilya, rather than to him or about him;
- the mention of him is finished business - somebody thanked him, agreed, or \
signed off, and nothing is being put to him;
- two people are settling something private, practical or logistical;
- it is greetings, one-word noise, stickers, links without comment;
- the subject is serious, sad, medical, financial or otherwise sensitive;
- he already made this point in the recent lines - repeating it is worse than \
silence.

When genuinely unsure, stay quiet. Silence costs nothing; a man who inserts \
himself into conversations that were not about him is exhausting.
"""

JUDGE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "speak": {"type": "BOOLEAN"},
        "reason": {"type": "STRING"},
    },
    "required": ["speak", "reason"],
}


def judge_order() -> List[Candidate]:
    """The rungs the judge may use - the DUMBEST first, climbing up.

    Deliberately the opposite direction from replies. A verdict is a yes/no, so
    the cheap rungs are good enough for it, and every judge call spent on a
    clever model is one the actual answers no longer have. It climbs only when
    a rung turns out unable to produce a usable verdict at all.

    GROUP_JUDGE_MODEL, when set to a model that is actually on the ladder, is
    tried before everything else.
    """
    usable = [c for c in LADDER if c.usable and not c.bad_judge]
    # The cheap end of the ladder is where models land for two different
    # reasons: being small, and being unsuitable. A reasoning model scores low
    # because it is wordy and slow - which puts it first in line for the judge,
    # the one job it is worst at. Skip them here; they are still fine for
    # replies. If somehow nothing else is left, a bad judge beats no judge.
    plain = [c for c in usable
             if not any(m in c.model.lower() for m in REASONING_MARKERS)]
    order = sorted(plain or usable, key=lambda c: c.quality)
    if GROUP_JUDGE_MODEL:
        pinned = [c for c in order if c.model == GROUP_JUDGE_MODEL]
        if pinned:
            order = pinned + [c for c in order if c not in pinned]
    return order


def judge_model() -> str:
    """Whichever rung the judge would reach for right now."""
    order = judge_order()
    return repr(order[0]) if order else "none"


def judge_budget_ok(key: str) -> bool:
    return rate_ok(judge_calls, key, GROUP_JUDGE_MAX_PER_MIN)


def should_speak(key: str, quiet_for: float, name_used: Optional[str] = None) -> Optional[str]:
    """Ask a cheap model whether Ilya would naturally jump in right now.

    Returns the reason to speak, "" when the judge says stay quiet, and None
    when the call itself failed (so the caller can fall back to keywords).
    """
    lines = [h["text"] if h["role"] == "user" else f"Ilya: {h['text']}"
             for h in list(history[key])[-GROUP_JUDGE_TURNS:]]
    transcript = "\n".join(lines)
    note = ""
    if name_used:
        note = (f'The last line contains the word "{name_used}". Decide whether '
                f"it is addressed to Ilya or merely mentions it.\n")
    question = (
        f"{transcript}\n\n---\n"
        f"{note}"
        f"Ilya last said something here {int(quiet_for)} seconds ago.\n"
        f"Should he say something now?"
    )

    # Same ladder as replies, walked the other way: cheapest rung first, and it
    # only climbs when one turns out no good at producing a verdict.
    for c in judge_order():
        verdict, outcome = judge_via(c, question)
        if verdict is not None:
            reason = str(verdict.get("reason", ""))[:80]
            if verdict.get("speak"):
                return f"context: {reason}"
            log.info("  staying quiet - %s", reason)
            return ""
        if outcome == "unusable":
            # It answered and still could not produce a verdict. It will do the
            # same next time, so stop asking it - for good.
            c.bad_judge = True
            log.info("  judge: %s cannot produce verdicts, dropping it", c)
        else:
            # Quota, network, a 5xx. Nothing to do with its judgement; skip it
            # this once and let the cooldown decide when it comes back.
            log.info("  judge: %s unavailable right now, trying the next rung", c)
    return None


def parse_json_loose(raw: str) -> Optional[dict]:
    """Models wrap JSON in prose, fences and apologies. Dig it out anyway."""
    try:
        return json.loads(raw)
    except Exception:
        pass
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    # Last resort: the only thing that matters is yes or no.
    low = raw.lower()
    if '"speak"' in low or "speak" in low:
        if re.search(r"speak\W{0,4}(true|yes)", low):
            return {"speak": True, "reason": "parsed from loose output"}
        if re.search(r"speak\W{0,4}(false|no)", low):
            return {"speak": False, "reason": "parsed from loose output"}
    return None


JUDGE_JSON_HINT = ('\n\nAnswer with JSON and nothing else - no prose, no code '
                   'fences: {"speak": true, "reason": "a few words"}')


def judge_via(c: Candidate, question: str) -> tuple:
    """Run the speak/stay-quiet decision on one rung.

    Returns (verdict, outcome). The outcome matters as much as the verdict:

      "ok"          - got a usable verdict
      "unusable"    - the model answered, but cannot produce a verdict at all
                      (prose instead of JSON, empty output). Worth remembering:
                      it will do the same thing next time.
      "unavailable" - the call itself did not land: network, 429, 5xx. Says
                      nothing about the model's ability to judge, so it must
                      NOT be held against it - the rung is simply skipped now
                      and tried again on the next message.
    """
    p = c.provider
    if p.name != "gemini":
        # Strict JSON mode is the good path, but several free models cannot
        # honour it and answer 400. Falling back to plain text plus a tolerant
        # parser is far better than losing the judge entirely.
        modes = (False,) if p.no_json else (True, False)
        answered = False
        for strict in modes:
            raw, status = openai_call(
                p, c.model, JUDGE_PROMPT + JUDGE_JSON_HINT,
                [{"role": "user", "content": question}],
                max_tokens=600, json_mode=strict, timeout=20,
            )
            if not raw:
                if status:
                    note_failure(c, status)      # a 429 here rests it properly
                continue
            answered = True
            verdict = parse_json_loose(raw)
            if verdict is not None:
                return verdict, "ok"
            log.warning("  judge: %s gave unparseable output", p.name)
        return None, ("unusable" if answered else "unavailable")

    model = c.model
    url, headers = gemini_url(model), gemini_headers()

    def body(thinking: bool) -> dict:
        gen: Dict[str, Any] = {
            "temperature": 0.2,
            "maxOutputTokens": 1024,
            "responseMimeType": "application/json",
            "responseSchema": JUDGE_SCHEMA,
        }
        if thinking:
            gen["thinkingConfig"] = {"thinkingLevel": "low"}
        return {
            "system_instruction": {"parts": [{"text": JUDGE_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": question}]}],
            "generationConfig": gen,
        }

    for thinking in (True, False):
        try:
            r = session.post(url, headers=headers, json=body(thinking), timeout=20)
        except Exception as exc:
            log.warning("  judge call failed: %s", exc)
            return None, "unavailable"
        if r.status_code == 400 and thinking and "think" in r.text.lower():
            continue
        if r.status_code != 200:
            log.warning("  judge %s: %s", r.status_code, r.text[:120].replace("\n", " "))
            note_failure(c, r.status_code)
            return None, "unavailable"
        try:
            return json.loads(gemini_text(r.json()["candidates"][0])), "ok"
        except Exception as exc:
            log.warning("  judge gave unusable output: %s", exc)
            return None, "unusable"
    return None, "unavailable"


def group_trigger(msg: dict, text: str, key: str) -> Optional[str]:
    """Why we should speak up in this group, or None to stay quiet."""
    if GROUP_REPLY_ALL or GROUP_TRIGGER == "all":
        return "reply-all is on"

    # Being spoken to directly always wins - no judging, no cooldown.
    replied = msg.get("reply_to_message") or {}
    if (replied.get("from") or {}).get("id") == BOT_ID:
        return "replying to us"
    if BOT_USERNAME and f"@{BOT_USERNAME}".lower() in text.lower():
        return "mentioned"

    # A name skips every guard below - but the judge still decides, because
    # "бот, расскажи анекдот" and "нам нужен бот для склада" look identical to
    # a regex and nothing alike to a reader.
    name = called_by_name(text)

    since = time.time() - last_interjection.get(key, 0)
    if not name:
        if GROUP_COOLDOWN and since < GROUP_COOLDOWN:
            log.info("%s | said something %.0fs ago, staying out of it "
                     "(GROUP_COOLDOWN=%.0fs)", key, since, GROUP_COOLDOWN)
            return None

        recent = list(history[key])[-GROUP_JUDGE_RECENT_TURNS:]
        in_the_exchange = any(h["role"] == "model" for h in recent)

        # A three-word jab right after he spoke is almost certainly aimed at
        # him - "дурак", "ну и бот", "заткнись". Those are exactly the messages
        # the length floor would throw away, so it only applies to a room he is
        # not part of.
        if len(text) < GROUP_JUDGE_MIN_CHARS and not in_the_exchange:
            return None
        if not GROUP_JOIN_TOPICS and not in_the_exchange:
            # Nobody can be referring to a man who is not in the conversation.
            return None

    if not judge_budget_ok(key):
        log.info("%s | judge budget spent for this minute", key)
        return None

    verdict = should_speak(key, since, name)
    if verdict:
        return verdict
    if verdict == "":
        return None                      # the judge deliberately said no
    # None means the call itself failed. Without a judge the only safe rule is:
    # answer if he was called by name, otherwise keep out of it.
    log.info("%s | judge unavailable, %s", key,
             "answering because a name was used" if name else "staying quiet")
    return f"called {name!r} (judge unavailable)" if name else None


def handle_group_message(msg: dict, cancel: Optional[threading.Event] = None) -> None:
    cancel = cancel if cancel is not None else NEVER_CANCELLED
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    sender = msg.get("from") or {}
    text = (msg.get("text") or msg.get("caption") or "").strip()

    # Every group message is traceable at DEBUG. At INFO this would flood a
    # busy room, but when the bot "does nothing" it is the first thing to look
    # at - set LOG_LEVEL=DEBUG and every message shows up with its verdict.
    log.debug("group %s | %s: %r", chat_id, display_name(sender), text[:60])

    if not text or sender.get("is_bot") or sender.get("id") in IGNORE_USER_IDS:
        return log.debug("  -> skipped: empty, from a bot, or an ignored user")
    if not group_allowed(chat_id):
        return

    # A room that passed the check is a room you are in. That is the set the
    # inline "shares a group with me" test is measured against.
    if chat_id not in known_groups:
        known_groups.add(chat_id)
        save_known_groups()
    # Everyone who talks here demonstrably shares this room with you, so they
    # can use the bot inline without a single membership call.
    allow_inline_for(sender.get("id"), chat_id)

    key = f"group:{chat_id}"

    # Strip our own @mention so the model doesn't answer its own username.
    clean = text
    if BOT_USERNAME:
        clean = re.sub(rf"@{re.escape(BOT_USERNAME)}\b", "", clean, flags=re.I).strip()

    # Everything said in the room is remembered, so that when we do speak we
    # know what the conversation has been about.
    remember(key, "user", f"{display_name(sender)}: {clean}")

    reason = group_trigger(msg, text, key)
    if not reason:
        log.debug("  -> nothing here for him to answer")
        return

    log.info("group %s | %s: %r  (%s)", chat_id, display_name(sender), clean[:60], reason)

    if REPLY_COOLDOWN and time.time() - last_reply_at.get(key, 0) < REPLY_COOLDOWN:
        log.info("  -> ignored: within REPLY_COOLDOWN")
        return

    log.info("  -> answering...")
    started = time.time()
    answer = ask_ai(key, None, GROUP_NOTE, fast=not GROUP_DELAY, room=True)
    if not answer:
        log.warning("  -> Gemini returned nothing, no reply sent")
        return

    # No quoting, and no human-typing theatre: in a room full of people a
    # 25-second pause just means the conversation has moved on without you.
    if GROUP_DELAY:
        if not pace_and_send(None, chat_id, clean, answer, None,
                             cancel, time.time() - started):
            return
    else:
        send_reply(None, chat_id, answer, None)
        log.info("  -> replied: %r", answer[:60])

    remember(key, "model", answer)
    last_reply_at[key] = time.time()
    if not reason.startswith(("mentioned", "replying")):
        last_interjection[key] = time.time()


def dispatch_group_message(msg: dict) -> None:
    """Unlike a 1:1 chat, a newer message here does NOT cancel the answer.

    In a group other people keep talking; that is the normal state of a room,
    not somebody correcting themselves. The chat lock still keeps replies in
    order; REPLY_COOLDOWN would space the replies out, but it is off by
    default so that two people addressing him at once both get an answer.
    """
    chat = msg.get("chat") or {}
    key = f"group:{chat.get('id')}"

    def run() -> None:
        with chat_lock(key):
            try:
                handle_group_message(msg)
            except Exception:
                log.exception("error answering group %s", chat.get("id"))

    run_off_poll_loop(run)


def dispatch_business_message(msg: dict) -> None:
    """Answer in a worker thread, but keep each chat strictly in order.

    A reply now takes ten to twenty seconds of deliberate typing. Doing that on
    the polling thread would freeze every other conversation for the duration.
    """
    chat = msg.get("chat") or {}
    key = f"{msg.get('business_connection_id')}:{chat.get('id')}"

    # Anything still being composed for this chat is now out of date - a newer
    # message has arrived, or this one replaced an edited original.
    cancel_pending(key, "a newer message arrived")

    entry = Pending(msg.get("message_id"))
    with _pending_guard:
        pending[key] = entry

    def run() -> None:
        with chat_lock(key):
            try:
                handle_business_message(msg, entry.cancel)
            except Exception:
                log.exception("error answering chat %s", chat.get("id"))

    run_off_poll_loop(run)


def probe_provider(p: Provider) -> tuple:
    """One tiny real request. Returns (ok, detail, seconds)."""
    started = time.time()

    if p.name == "gemini":
        try:
            r = session.post(
                gemini_url(GEMINI_MODEL), headers=gemini_headers(p.key),
                json={"contents": [{"role": "user", "parts": [{"text": "Reply with OK"}]}],
                      "generationConfig": {"maxOutputTokens": 1024, "temperature": 0}},
                timeout=25,
            )
        except Exception as exc:
            return False, f"network error: {exc}", time.time() - started
        took = time.time() - started
        return (True, GEMINI_MODEL, took) if r.status_code == 200 else (
            False, http_error(r), took)

    headers = provider_headers(p)
    try:
        r = session.post(
            f"{p.base}/chat/completions", headers=headers,
            json={"model": p.model, "max_tokens": 16, "temperature": 0,
                  "messages": [{"role": "user", "content": "Reply with OK"}]},
            timeout=25,
        )
    except Exception as exc:
        return False, f"network error: {exc}", time.time() - started
    took = time.time() - started
    if r.status_code == 200:
        return True, p.model, took

    detail = http_error(r)
    # A dead model name is the most common failure, and the fix is knowable:
    # ask the provider what it actually serves.
    if looks_like_model_error(r.status_code, r.text):
        candidates = replacement_models(p, r.text)
        if r.status_code == 402:
            candidates.sort(key=model_size)
        failures = []
        saw_payment = r.status_code == 402
        for alt in candidates:
            try:
                r2 = session.post(
                    f"{p.base}/chat/completions", headers=headers,
                    json={"model": alt, "max_tokens": 16, "temperature": 0,
                          "messages": [{"role": "user", "content": "Reply with OK"}]},
                    timeout=25,
                )
            except Exception as exc:
                failures.append((alt, 0, str(exc)[:60]))
                continue
            if r2.status_code == 200:
                dead, p.model = p.model, alt
                log.warning("check: %s switched %s -> %s", p.name, dead, alt)
                return (True,
                        f"{alt}  (was {dead}; pin it with {p.name.upper()}_MODEL)",
                        time.time() - started)
            failures.append((alt, r2.status_code, http_error(r2)))
            if r2.status_code == 402 and not saw_payment:
                saw_payment = True
                # Everything from here on: cheapest first.
                candidates.sort(key=model_size)

        if not failures:
            return False, detail + " - and it lists no usable chat model", took

        codes = {code for _, code, _ in failures}
        # If every alternative is refused the same way, the model was never the
        # problem - say what the real one is instead of listing dead names.
        if codes <= {401, 403}:
            verdict = ("the key itself is being refused (%s) - check it is "
                       "active and has inference permission" % failures[0][2])
        elif codes <= {402}:
            verdict = ("every model this key can see is paid - the free tier "
                       "covers smaller ones, set %s_MODEL by hand from the "
                       "provider's free list" % p.name.upper())
        elif codes == {404} or codes == {404, 410}:
            verdict = ("this key has access to none of them: "
                       + ", ".join(a for a, _, _ in failures[:4]))
        else:
            verdict = "; ".join(f"{a} -> {m}" for a, _, m in failures[:2])
        return False, f"{detail} - tried {len(failures)} alternatives, {verdict}", took
    return False, detail, took


def http_error(r: Any) -> str:
    try:
        err = r.json().get("error", {})
        msg = err.get("message") if isinstance(err, dict) else str(err)
    except Exception:
        msg = None
    msg = (msg or r.text or "")[:120].replace("\n", " ").strip()
    return f"HTTP {r.status_code}: {msg}" if msg else f"HTTP {r.status_code}"


def check_providers() -> str:
    """Probe every configured provider and describe what happened."""
    if not PROVIDERS:
        return "No AI providers configured at all - the bot cannot answer anything."

    lines, working = [], 0
    for p in PROVIDERS:
        ok, detail, took = probe_provider(p)
        if ok:
            p.revive()
            working += 1
            lines.append(f"OK    {p.name:<11} {detail}  ({took:.1f}s)")
        else:
            lines.append(f"FAIL  {p.name:<11} {detail}")
        log.info("check: %s %s", p.name, "ok" if ok else f"failed - {detail}")

    head = f"{working} of {len(PROVIDERS)} providers answered."
    if working == 0:
        head += " Nothing will work until one of them does."
    elif working < len(PROVIDERS):
        head += " The working ones cover for the rest."
    return head + "\n\n" + "\n".join(lines)


def status_report() -> str:
    lines = [
        f"locked to owner: {OWNER_ID or 'NO - anyone can use this bot'}",
        f"group trigger: {GROUP_TRIGGER}"
        + (f" via {judge_model()}" if GROUP_TRIGGER == "context" else "")
        + (", may join topics" if GROUP_JOIN_TOPICS else ", only when addressed"),
        "parked: " + (", ".join(
            f"{p.name} ({int(p.parked_until - time.time())}s)"
            for p in PROVIDERS if p.parked) or "none"),
        "group messages: " + (
            "all visible"
            if BOT_SEES_ALL_GROUP_MESSAGES
            else "PRIVACY MODE ON - only mentions and replies arrive, so he "
                 "cannot see jabs or topics (see /setprivacy in BotFather)"
        ),
        "inline: " + (
            "off (INLINE_ENABLED=false)" if not INLINE_ENABLED else
            f"{INLINE_ACCESS}, {inline_offered} offered / {inline_sent} sent"
            + (f", groups known: {len(known_groups)}"
               if INLINE_ACCESS == "shared" else "")
        ),
        "judge (cheapest first): " + (
            ", ".join(f"{c.provider.name}/{c.model}" for c in judge_order()[:3])
            or "nothing usable") + (
            f"   [{sum(1 for c in LADDER if c.bad_judge)} dropped as unable "
            f"to judge]" if any(c.bad_judge for c in LADDER) else ""),
        "ladder (best first):",
    ] + [
        "  {}{:<40} q={}{}".format(
            "   " if c.usable else "x  ",
            f"{c.provider.name}/{c.model}",
            c.quality,
            f" {c.seconds:.0f}s" if c.seconds else "",
        ) + ("" if c.usable else
             "  dead" if c.dead else
             f"  resting {int(c.cool_until - time.time())}s" if c.cool_until > time.time()
             else "  provider parked")
        for c in ladder_order()[:10]
    ] + [
        f"uptime: {int((time.time() - STARTED_AT) / 60)} min",
        f"business connections known: {len(owner_of_connection)}",
    ]
    for cid, oid in owner_of_connection.items():
        lines.append(f"  {cid[:12]}... owner={oid} can_reply={can_reply.get(cid)}")
    if not owner_of_connection:
        lines.append(
            "  none yet - the bot learns this on the first business message, "
            "or when you re-add it under Telegram Business > Chatbots"
        )
    lines.append(f"active chats in memory: {len(history)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Inline mode - summoning him in a chat he is not a member of
# --------------------------------------------------------------------------
#
# Telegram delivers "@thebot whatever" from ANY chat straight to the bot; it
# does not need to be in that chat, or in any chat. What it does NOT deliver is
# which chat that was. An inline query carries the sender, the text and a coarse
# chat_type - never a chat id - so "only in chats I am in" is not something a
# bot can enforce. What it can check is the person: INLINE_ACCESS=shared serves
# anyone who sits in one of the groups you are in, which is the same guarantee
# from the other side.
#
# You send first, he answers after. Typing costs nothing: every keystroke gets
# the same instant stub back. The model runs once, when you actually send the
# message, and the sent message is then edited into the reply. That is one call
# per message sent - not per message typed, and not per letter.
#
# Telegram only reports which result you picked, and only hands over the
# inline_message_id needed to edit it, if the result carried an inline keyboard.
# So the stub gets a one-button hourglass, and the edit takes it away again.

# user id -> (allowed, checked at). Membership does not change by the minute.
inline_ok_cache: Dict[int, Tuple[bool, float]] = {}

# When each person last summoned him, for INLINE_MAX_PER_MIN.
inline_calls: Dict[int, Deque[float]] = defaultdict(deque)

# Groups the owner is in and the bot can see. The Bot API cannot list a bot's
# chats, so this fills in as people talk - and is persisted, so a restart does
# not lock everyone but you out until somebody happens to write.
known_groups: set = set()

inline_offered = 0
inline_sent = 0
inline_seen = False        # has any inline query ever arrived this run?


def allow_inline_for(user_id: int, chat_id: int) -> None:
    """Record that this person shares a group with you.

    Called for free whenever somebody speaks in one of your groups - we already
    know the room is yours by then, so no getChatMember is needed at all. That
    means the people who actually talk are warm in the cache before they ever
    try to summon him.
    """
    if not user_id or user_id == OWNER_ID:
        return
    if inline_ok_cache.get(user_id, (False, 0))[0]:
        return                                   # already known, don't re-write
    inline_ok_cache[user_id] = (True, time.time() + INLINE_MEMBER_DAYS * 86400)
    save_shared_member(user_id, chat_id)


def may_use_inline(user_id: Optional[int]) -> bool:
    """Is this person allowed to summon him?

    "shared" means: do they sit in any room you sit in. A yes is kept for
    INLINE_MEMBER_DAYS and mirrored into Redis, so it survives a restart and
    the whole group keeps working; a no is kept for minutes, so somebody who
    joins tomorrow is not locked out until next week.
    """
    if not user_id:
        return False
    if not OWNER_ID or user_id == OWNER_ID or INLINE_ACCESS == "all":
        return True
    if INLINE_ACCESS == "owner":
        return False

    now = time.time()
    cached = inline_ok_cache.get(user_id)
    if cached and now < cached[1]:
        return cached[0]

    # Redis first: another instance, or this one before the last redeploy, may
    # already have paid the API calls for this person.
    seen_in = load_shared_member(user_id)
    if seen_in is not None:
        inline_ok_cache[user_id] = (True, now + INLINE_MEMBER_DAYS * 86400)
        return True

    # Otherwise ask Telegram, one group at a time. First hit wins, so this is
    # normally a single call - and only once a week per person.
    for chat_id in list(known_groups):
        res = tg("getChatMember", chat_id=chat_id, user_id=user_id)
        if res and res.get("status") not in ("left", "kicked"):
            allow_inline_for(user_id, chat_id)
            log.info("inline: %s shares group %s with you - allowed for %d days",
                     user_id, chat_id, INLINE_MEMBER_DAYS)
            return True

    inline_ok_cache[user_id] = (False, now + INLINE_MISS_MINUTES * 60)
    log.info("inline: %s shares no group with you - turned away for %d min",
             user_id, INLINE_MISS_MINUTES)
    return False


def answer_nothing(query_id: str, note: str, seconds: int = 0) -> None:
    """An empty result with a line explaining why - better than a silent bot."""
    tg("answerInlineQuery", inline_query_id=query_id, results=[],
       cache_time=seconds, is_personal=True,
       button={"text": note, "start_parameter": "inline"})


def handle_inline_query(q: dict) -> None:
    """Instant and free. Nothing is generated until the message is actually sent."""
    global inline_seen
    query_id = q.get("id")
    sender = q.get("from") or {}
    user_id = sender.get("id")
    text = (q.get("query") or "").strip()

    # The first one is worth an INFO line: it is the only proof that inline
    # mode is wired up at all, and "I typed @thebot and nothing happened" is
    # otherwise indistinguishable from a bot that never got the query.
    if not inline_seen:
        inline_seen = True
        log.info("inline query received from %s (%r) - the panel is working; "
                 "tap the result to actually send it", user_id, text[:40])
    else:
        log.debug("inline query from %s: %r", user_id, text[:60])

    if not text:
        return answer_nothing(query_id, "Напиши, на что ответить")
    if not may_use_inline(user_id) or user_id in IGNORE_USER_IDS:
        return answer_nothing(query_id, "This bot is private.", 300)

    global inline_offered
    inline_offered += 1
    tg(
        "answerInlineQuery",
        inline_query_id=query_id,
        cache_time=0,
        is_personal=True,
        results=[{
            "type": "article",
            "id": hashlib.md5(text.encode("utf-8")).hexdigest(),
            "title": "Ответить",
            "description": text[:120],
            "input_message_content": {"message_text": INLINE_PLACEHOLDER},
            # Required: without a keyboard Telegram reports neither the choice
            # nor the inline_message_id, and the stub could never be filled in.
            "reply_markup": {"inline_keyboard": [[
                {"text": "⏳", "callback_data": "wait"}]]},
        }],
    )


def handle_chosen_inline_result(chosen: dict) -> None:
    """Sent. Now the model runs and the posted stub becomes the reply."""
    global inline_sent
    inline_sent += 1

    inline_message_id = chosen.get("inline_message_id")
    text = (chosen.get("query") or "").strip()
    user_id = (chosen.get("from") or {}).get("id")

    if not may_use_inline(user_id) or user_id in IGNORE_USER_IDS:
        return
    if not inline_message_id:
        log.warning("chosen inline result without inline_message_id - the result "
                    "was built without a keyboard, so it cannot be filled in")
        return
    if not rate_ok(inline_calls, user_id, INLINE_MAX_PER_MIN):
        log.info("inline: %s is over %d/min", user_id, INLINE_MAX_PER_MIN)
        tg("editMessageText", inline_message_id=inline_message_id,
           text="Слишком часто. Подожди минуту.")
        return

    log.info("inline from %s: %r", user_id, text[:60])
    started = time.time()
    # Same rules as a room he actually sits in: he takes two or three sentences
    # and gives as good as he gets - minus the parts that assume he can see it.
    # The one place with a real cap: an inline message is edited in place, so
    # it cannot be split across several messages the way sendMessage is.
    answer = ask_ai(INLINE_KEY, text[:MAX_INPUT_CHARS], INLINE_NOTE, fast=True,
                    room=True, max_chars=TELEGRAM_MAX_CHARS - 96)

    if not answer:
        log.warning("  -> nothing came back")
        # Never leave an ellipsis sitting in somebody else's chat.
        tg("editMessageText", inline_message_id=inline_message_id,
           text="Не сейчас.")
        return

    if tg("editMessageText", inline_message_id=inline_message_id, text=answer) is None:
        log.warning("  -> could not fill in the message")
        return
    log.info("  -> inline answer in %.1fs: %r", time.time() - started, answer[:60])


def dispatch_inline_choice(chosen: dict) -> None:
    def run() -> None:
        try:
            handle_chosen_inline_result(chosen)
        except Exception:
            log.exception("error answering an inline query")

    run_off_poll_loop(run)


_inline_feedback_warned = False


def warn_if_inline_feedback_off() -> None:
    """The one inline setting the API will not report.

    /setinlinefeedback controls whether Telegram says which result was picked.
    With it off the stub is posted and nothing ever fills it in - and there is
    no error anywhere, because from the bot's side nothing happened. Offering a
    pile of results and never being told one was sent is the symptom.
    """
    global _inline_feedback_warned
    if _inline_feedback_warned or inline_sent or inline_offered < 5:
        return
    _inline_feedback_warned = True
    log.warning(
        "offered %d inline replies and Telegram never said one was sent. If the "
        "message in the chat is stuck on '%s', inline feedback is off: "
        "@BotFather -> /setinlinefeedback -> @%s -> Enabled.",
        inline_offered, INLINE_PLACEHOLDER, BOT_USERNAME,
    )


def handle_update(update: dict) -> None:
    if "inline_query" in update:
        if INLINE_ENABLED:
            handle_inline_query(update["inline_query"])
        return
    if "chosen_inline_result" in update:
        if INLINE_ENABLED:
            dispatch_inline_choice(update["chosen_inline_result"])
        return
    if "callback_query" in update:
        # The hourglass on a stub that is still being filled in. Acknowledge it
        # so the sender's client stops spinning.
        tg("answerCallbackQuery", callback_query_id=update["callback_query"].get("id"),
           text="Секунду.")
        return
    if "business_connection" in update:
        remember_connection(update["business_connection"])
        return
    if "business_message" in update:
        dispatch_business_message(update["business_message"])
        return
    if "edited_business_message" in update:
        msg = update["edited_business_message"]
        chat = msg.get("chat") or {}
        key = f"{msg.get('business_connection_id')}:{chat.get('id')}"
        with _pending_guard:
            p = pending.get(key)
            mid = msg.get("message_id")
            answering_it = bool(p and p.message_id == mid)
        if answering_it:
            # They fixed it before we answered - throw our draft away and read
            # the new version from scratch.
            log.info("message %s edited mid-answer, starting over", mid)
            dispatch_business_message(msg)
        else:
            log.info("edited business message %s, already answered - ignored", mid)
        return

    if "deleted_business_messages" in update:
        d = update["deleted_business_messages"]
        chat = d.get("chat") or {}
        key = f"{d.get('business_connection_id')}:{chat.get('id')}"
        ids = set(d.get("message_ids") or [])
        with _pending_guard:
            p = pending.get(key)
            mine = p.message_id if p else None
        if mine in ids:
            # They took it back before we answered. Say nothing and wait for
            # whatever they write next.
            log.info("message %s deleted before the reply went out", mine)
            cancel_pending(key, "the message was deleted", only_message_id=mine)
        return

    msg = update.get("message")
    if not msg:
        log.info("update ignored (%s)", ", ".join(k for k in update if k != "update_id"))
        return

    # Group chats: the bot is a plain member here, not a business assistant.
    if (msg.get("chat") or {}).get("type") in ("group", "supergroup"):
        if GROUPS_ENABLED:
            dispatch_group_message(msg)
        return

    # A normal DM to the bot itself - handy for checking it is alive.
    text = (msg.get("text") or "").strip()
    sender_id = (msg.get("from") or {}).get("id")
    is_owner = not OWNER_ID or sender_id == OWNER_ID

    if not is_owner:
        # Somebody else found the bot. Don't hand them diagnostics.
        log.info("DM from %s (not the owner), turned away", sender_id)
        if text.startswith("/"):
            tg("sendMessage", chat_id=msg["chat"]["id"],
               text="This bot is private.")
        return

    if text.startswith("/check"):
        chat_id = msg["chat"]["id"]
        tg("sendMessage", chat_id=chat_id,
           text=f"Checking {len(PROVIDERS)} provider(s), one real request each...")

        def run_check() -> None:
            try:
                tg("sendMessage", chat_id=chat_id, text=check_providers())
            except Exception:
                log.exception("provider check failed")
                tg("sendMessage", chat_id=chat_id, text="The check itself broke - see the log.")

        # Probing five providers can take a minute; never block the poll loop.
        run_off_poll_loop(run_check)
    elif text.startswith("/status"):
        tg("sendMessage", chat_id=msg["chat"]["id"], text=status_report())
    elif text.startswith("/start"):
        tg(
            "sendMessage",
            chat_id=msg["chat"]["id"],
            text=(
                "I'm alive. Connect me under Settings -> Telegram Business -> "
                "Chatbots and I'll answer your chats for you.\n\n"
                f"In any other chat - even one I'm not in - type "
                f"'@{BOT_USERNAME} ' and the line you want answered. The reply "
                "appears above the input box; tap it to send it as your own "
                "message.\n\n"
                "/status - what I currently know\n"
                "/check  - test every AI provider key"
            ),
        )


# --------------------------------------------------------------------------
# Health endpoint (some free hosts require an open port)
# --------------------------------------------------------------------------


def start_health_server() -> None:
    port = os.environ.get("PORT")
    if not port:
        return
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_):  # silence access logs
            pass

    srv = HTTPServer(("0.0.0.0", int(port)), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("health endpoint listening on :%s", port)


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

# Telegram allows exactly one getUpdates consumer per token. A second one -
# the previous Render deploy still shutting down, a forgotten local run, a
# duplicated service - takes the updates away from this one, which then sees
# nothing at all: no messages, no errors, nothing in the log to explain it.
# That is worth saying out loud, once, instead of a warning every three
# seconds that reads like a network hiccup.
_conflict_since = 0.0


def poll_updates(offset: int) -> Optional[list]:
    """One getUpdates, with the "somebody else is polling" case spelled out."""
    global _conflict_since
    try:
        r = session.post(
            f"{TELEGRAM_API}/getUpdates", timeout=70,
            json={"offset": offset, "timeout": 50,
                  "allowed_updates": ALLOWED_UPDATES},
        )
        data = r.json()
    except Exception as exc:
        log.warning("getUpdates failed: %s", exc)
        return None

    if data.get("ok"):
        if _conflict_since:
            log.warning("the other instance is gone after %.0fs - this one is "
                        "receiving messages again", time.time() - _conflict_since)
            _conflict_since = 0.0
        return data.get("result") or []

    description = str(data.get("description") or "")
    if "conflict" in description.lower():
        if not _conflict_since:
            _conflict_since = time.time()
            log.error(
                "ANOTHER INSTANCE OF THIS BOT IS RUNNING. Telegram delivers "
                "each message to ONE poller, and it is not this one - which is "
                "why nothing appears here however much you write. Usually the "
                "previous deploy that has not shut down yet (give it a minute), "
                "a second Render service on the same TELEGRAM_BOT_TOKEN, or a "
                "copy still running on your laptop. Two bots on one token can "
                "never both work; stop one."
            )
        else:
            # Already said it. Keep the log readable while it resolves itself.
            log.info("still waiting for the other instance to stop (%.0fs)",
                     time.time() - _conflict_since)
        time.sleep(10)
        return None

    log.warning("getUpdates error: %s", description)
    return None


ALLOWED_UPDATES = [
    "message",
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
    # Inline mode. chosen_inline_result is what makes it work at all - it only
    # arrives if inline feedback is on in BotFather, which is why the bot
    # watches for its absence at runtime.
    "inline_query",
    "chosen_inline_result",
    "callback_query",
]


def main() -> None:
    # Open the port first: hosts that scan for a listening socket (Render free
    # web services) mark the deploy as failed if nothing binds quickly.
    start_health_server()

    if not TELEGRAM_TOKEN:
        sys.exit("Set TELEGRAM_BOT_TOKEN (see .env.example).")
    if not PROVIDERS:
        sys.exit(
            "No AI provider configured. Set at least one of GEMINI_API_KEY, "
            "GROQ_API_KEY, CEREBRAS_API_KEY, OPENROUTER_API_KEY, "
            "MISTRAL_API_KEY, GITHUB_MODELS_TOKEN (see .env.example)."
        )
    log.info("AI providers, in order: %s",
             ", ".join(f"{p.name}/{p.model or 'auto'}" for p in PROVIDERS))

    global BOT_ID, BOT_USERNAME, BOT_SEES_ALL_GROUP_MESSAGES
    me = tg("getMe")
    if not me:
        sys.exit("Telegram rejected the token. Check TELEGRAM_BOT_TOKEN.")
    BOT_ID = me.get("id")
    BOT_USERNAME = me.get("username") or ""
    log.info("logged in as @%s (id %s)", BOT_USERNAME, BOT_ID)

    if OWNER_ID:
        log.info("locked to owner %s - every other account is ignored", OWNER_ID)
    else:
        log.warning(
            "OWNER_ID is not set: anyone who knows @%s can attach it to their "
            "own business account and spend your Gemini quota. Set OWNER_ID to "
            "your Telegram user id.", BOT_USERNAME,
        )
    if GROUPS_ENABLED:
        log.info("group trigger mode: %s", GROUP_TRIGGER)
        # Telegram only delivers ordinary group messages to a bot whose privacy
        # mode is off. Without that the bot literally never sees the text, so
        # keyword triggers and reply-all cannot fire - and nothing appears in
        # the log to explain why.
        sees_everything = bool(me.get("can_read_all_group_messages"))
        BOT_SEES_ALL_GROUP_MESSAGES = sees_everything
        log.info(
            "groups: on, replying when @%s is mentioned, replied to, called by "
            "name, or talked about%s", BOT_USERNAME,
            "; and to every message (reply-all)" if GROUP_REPLY_ALL
            else "; may also join topics" if GROUP_JOIN_TOPICS else "",
        )
        if not sees_everything:
            log.warning(
                "PRIVACY MODE IS ON: Telegram is not delivering ordinary group "
                "messages to this bot, so keyword triggers will never fire. "
                "Fix: @BotFather -> /setprivacy -> @%s -> Disable, then REMOVE "
                "the bot from the group and add it back (the setting is only "
                "applied when it joins).", BOT_USERNAME,
            )
        else:
            log.info("privacy mode off - all group messages are visible")

    if INLINE_ENABLED:
        if INLINE_ACCESS not in ("owner", "shared", "all"):
            log.warning("INLINE_ACCESS=%r is not owner|shared|all - treating it "
                        "as 'owner'", INLINE_ACCESS)
        # An allowlist is a definitive answer to "which groups are yours", so
        # strangers can be judged against it from the first second. Anything
        # learned in an earlier run is remembered too, so a restart does not
        # lock everyone but you out.
        known_groups.update(GROUP_ALLOWLIST)
        load_known_groups()
        if me.get("supports_inline_queries"):
            log.info(
                "inline mode on (access: %s) - type '@%s ...' in ANY chat, even "
                "one this bot was never added to", INLINE_ACCESS, BOT_USERNAME,
            )
            if INLINE_ACCESS == "shared" and not known_groups:
                log.info(
                    "  no groups known yet, so only you can use it inline until "
                    "somebody writes in one of your groups (or set "
                    "GROUP_ALLOWLIST to name them up front)"
                )
            elif INLINE_ACCESS == "all":
                log.warning(
                    "  INLINE_ACCESS=all: anyone who knows @%s can spend your "
                    "quota, capped only by INLINE_MAX_PER_MIN=%d per person",
                    BOT_USERNAME, INLINE_MAX_PER_MIN,
                )
        else:
            log.warning(
                "INLINE MODE IS OFF: typing '@%s ...' in another chat will find "
                "nothing. Fix: @BotFather -> /setinline -> @%s -> send a "
                "placeholder line like 'что ответить...'.",
                BOT_USERNAME, BOT_USERNAME,
            )

    if any(p.name == "gemini" for p in PROVIDERS):
        pick_working_model()
    build_ladder()

    # Only now does the ladder exist, so this is the first point at which the
    # judge's rung can be named or GROUP_JUDGE_MODEL can be checked at all.
    if GROUPS_ENABLED and GROUP_TRIGGER == "context":
        log.info("group judge starts on %s and climbs only if it has to",
                 judge_model())
        if GROUP_JUDGE_MODEL and not any(c.model == GROUP_JUDGE_MODEL for c in LADDER):
            log.warning(
                "GROUP_JUDGE_MODEL=%r is not on the ladder, so it is ignored "
                "and the judge just starts at the top. Check the spelling "
                "against the ladder printed above.", GROUP_JUDGE_MODEL,
            )

    if bool(UPSTASH_URL) != bool(UPSTASH_TOKEN):
        log.warning(
            "only half of the Upstash credentials are set (%s missing) - "
            "falling back to the file, which does not survive a redeploy",
            "UPSTASH_REDIS_REST_TOKEN" if UPSTASH_URL else "UPSTASH_REDIS_REST_URL",
        )

    load_history()
    if redis_on() or HISTORY_FILE:
        threading.Thread(target=history_saver, daemon=True).start()
        where = f"redis ({UPSTASH_URL.split('//')[-1]})" if redis_on() else HISTORY_FILE
        log.info("history persisted to %s every %ds", where, HISTORY_SAVE_EVERY)
        # Render and Docker stop a container with SIGTERM; write before dying.
        signal.signal(signal.SIGTERM, lambda *_: (save_history(), sys.exit(0)))

    # Keep the backlog. On a host that sleeps, the messages that arrived while
    # the instance was down are queued here - dropping them means the customer
    # is silently ignored. MAX_MESSAGE_AGE filters out anything truly stale.
    tg("deleteWebhook", drop_pending_updates=False)
    offset = 0

    log.info("polling for business messages...")
    while True:
        updates = poll_updates(offset)
        if updates is None:
            time.sleep(3)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            try:
                handle_update(update)
            except Exception:
                log.exception("error handling update %s", update.get("update_id"))
        if INLINE_ENABLED:
            warn_if_inline_feedback_off()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        save_history()
        log.info("bye")
