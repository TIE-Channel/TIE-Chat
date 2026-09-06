#!/usr/bin/env python3
"""
Telegram Business auto-responder powered by Google Gemini (free tier).

Listens for messages sent to your Telegram Business account by customers and
replies automatically, in the same language the customer wrote in, keeping
per-chat conversation history.

Runs with plain long-polling: no public URL, no webhook, no framework.
"""

from __future__ import annotations

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
from typing import Any, Deque, Dict, List, Optional

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
REPLY_COOLDOWN = float(os.environ.get("REPLY_COOLDOWN", "2"))

# Max characters of a customer message we forward to the model.
MAX_INPUT_CHARS = int(os.environ.get("MAX_INPUT_CHARS", "4000"))

# Ignore messages older than this (seconds). Matters on hosts that sleep:
# Telegram holds updates for ~24h and delivers the lot when the bot wakes up.
MAX_MESSAGE_AGE = int(os.environ.get("MAX_MESSAGE_AGE", "3600"))

# Gemini generation settings. maxOutputTokens covers thinking AND the answer,
# so keep it comfortably above what a short reply needs.
TEMPERATURE = float(os.environ.get("TEMPERATURE", "1.0"))
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "2048"))
GEMINI_TIMEOUT = int(os.environ.get("GEMINI_TIMEOUT", "45"))

# Hard ceiling on what actually gets sent. The persona asks for one line; this
# is the seatbelt for when a model ignores that entirely.
REPLY_MAX_CHARS = int(os.environ.get("REPLY_MAX_CHARS", "1200"))

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
GROUP_COOLDOWN = float(
    os.environ.get("GROUP_COOLDOWN")
    or os.environ.get("GROUP_KEYWORD_COOLDOWN")   # previous name, still honoured
    or "0"
)

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
# OpenAI-compatible, so one client covers all of them. Providers are tried in
# order and the first one that answers wins, which means a 429 on the free tier
# is a half-second detour instead of a dead chat.
# --------------------------------------------------------------------------

class Provider:
    __slots__ = ("name", "base", "key", "model", "searched", "fails",
                 "parked_until", "no_json", "no_reasoning_param")

    def __init__(self, name: str, base: str, key: str, model: str) -> None:
        self.name, self.base, self.key, self.model = name, base, key, model
        self.searched = False        # have we already hunted for a live model?
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

    def __repr__(self) -> str:
        return f"{self.name}({self.model})"


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

# history[(business_connection_id, chat_id)] -> deque of {"role","text"}
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

# Primary model first, lighter flash models behind it as live fallbacks.
MODEL_CANDIDATES: List[str] = []
PRIMARY_MODEL = GEMINI_MODEL
PINNED_UNTIL = 0.0
FALLBACK_MINUTES = int(os.environ.get("FALLBACK_MINUTES", "15"))

EXECUTOR = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="reply")
ASYNC_REPLIES = WORKERS > 1

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
    global GEMINI_MODEL, MODEL_CANDIDATES, PRIMARY_MODEL
    try:
        r = session.get(
            f"{GEMINI_API}/models",
            headers={"x-goog-api-key": GEMINI_API_KEY},
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
    PRIMARY_MODEL = GEMINI_MODEL
    log.info("using Gemini model %s (fallbacks: %s)",
             GEMINI_MODEL, ", ".join(MODEL_CANDIDATES[1:3]) or "none")
    return GEMINI_MODEL


# Model names churn constantly, and a provider's catalogue is full of things
# that are not chatbots. These never are.
NOT_A_CHAT_MODEL = (
    "guard", "whisper", "tts", "embed", "rerank", "moderation", "safety",
    "ocr", "asr", "transcribe", "diffusion", "image", "vision", "audio",
    "speech", "reward", "classifier",
)


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
    if any(slow in n for slow in ("reasoning", "thinking", "-r1", "deepthink")):
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


def trim_reply(text: str) -> str:
    """Last-resort length guard, cutting at a sentence end where possible."""
    if len(text) <= REPLY_MAX_CHARS:
        return text
    head = text[:REPLY_MAX_CHARS]
    cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "),
              head.rfind(".\n"), head.rfind("…"))
    return (head[:cut + 1] if cut > REPLY_MAX_CHARS // 3 else head).strip()


def openai_turns(key: str, user_text: Optional[str]) -> List[dict]:
    msgs = [
        {"role": "user" if h["role"] == "user" else "assistant", "content": h["text"]}
        for h in history[key]
    ]
    if user_text is not None:
        msgs.append({"role": "user", "content": user_text})
    return msgs


def openai_chat(
    p: Provider,
    system: str,
    messages: List[dict],
    max_tokens: int,
    json_mode: bool = False,
    timeout: Optional[int] = None,
) -> Optional[str]:
    """One call to any OpenAI-compatible endpoint. None means it did not work."""
    body: Dict[str, Any] = {
        "model": p.model,
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
    headers = {"Authorization": f"Bearer {p.key}", "Content-Type": "application/json"}
    if p.name == "openrouter":                      # OpenRouter asks for these
        headers["HTTP-Referer"] = "https://t.me"
        headers["X-Title"] = "telegram-business-bot"

    started = time.time()
    try:
        r = session.post(f"{p.base}/chat/completions", headers=headers, json=body,
                         timeout=timeout or GEMINI_TIMEOUT)
    except Exception as exc:
        log.warning("  -> %s failed after %.0fs: %s", p.name, time.time() - started, exc)
        return None

    if r.status_code != 200:
        if (r.status_code == 400 and "reasoning_format" in r.text
                and not p.no_reasoning_param):
            p.no_reasoning_param = True
            log.info("  -> %s rejects reasoning_format, retrying without it", p.name)
            return openai_chat(p, system, messages, max_tokens, json_mode, timeout)
        if json_mode and r.status_code == 400 and "json" in r.text.lower():
            # This model cannot honour response_format. Note it once and stop
            # paying for the lesson on every future call.
            if not p.no_json:
                log.warning("  -> %s cannot do strict JSON mode, using plain text",
                            p.name)
            p.no_json = True
            return None
        log.warning("  -> %s %s: %s", p.name, r.status_code,
                    r.text[:140].replace("\n", " "))
        # A dead model name is fixable without you: find a live one and retry.
        if looks_like_model_error(r.status_code, r.text) and not p.searched:
            p.searched = True
            dead = p.model
            queue = replacement_models(p, r.text)
            if r.status_code == 402:
                queue.sort(key=model_size)      # free tiers give away the small ones
            while queue:
                alt = queue.pop(0)
                log.warning("  -> %s: %s unusable, trying %s", p.name, p.model, alt)
                p.model = alt
                before = time.time()
                answer = openai_chat(p, system, messages, max_tokens, json_mode, timeout)
                if answer:
                    log.warning("  -> %s now on %s (set %s_MODEL to keep it)",
                                p.name, alt, p.name.upper())
                    return answer
                del before
            p.model = dead
        return None
    try:
        text = r.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        log.warning("  -> %s gave unusable output: %s", p.name, exc)
        return None
    raw_len = len(text or "")
    text = strip_thinking(text or "")
    if raw_len and not text:
        log.warning("  -> %s sent %d chars of pure reasoning and no answer",
                    p.name, raw_len)
        return None
    if len(text) < raw_len:
        log.info("  -> stripped %d chars of <think> from %s",
                 raw_len - len(text), p.name)
    if not text:
        log.warning("  -> %s returned an empty message", p.name)
        return None
    if not json_mode:
        text = trim_reply(text)
    log.info("  -> %s 200 in %.1fs, %d chars", p.name, time.time() - started, len(text))
    return text


def ask_ai(
    key: str,
    user_text: Optional[str],
    extra_system: str = "",
    cancel: Optional[threading.Event] = None,
) -> Optional[str]:
    """Ask the providers in order; the first one that answers wins."""
    if not PROVIDERS:
        log.error("no AI providers configured - set at least one API key")
        return None

    system = PERSONA + SYSTEM_SUFFIX + extra_system + now_line()
    live = [p for p in PROVIDERS if not p.parked]
    if not live:                       # everyone is parked - try them anyway
        live = PROVIDERS
        for p in live:
            p.parked_until = 0.0

    for p in live:
        if cancel is not None and cancel.is_set():
            return None
        if p.name == "gemini":
            text = ask_gemini(key, user_text, extra_system, cancel)
        else:
            text = openai_chat(p, system, openai_turns(key, user_text),
                               MAX_OUTPUT_TOKENS)
        if text:
            p.revive()
            if p is not live[0]:
                log.warning("  -> answered by %s (the ones before it were busy)", p.name)
            return text
        p.park()
        log.warning("  -> %s could not answer, moving on", p.name)
    log.error("  -> every provider failed")
    return None


def ask_gemini(
    key: str,
    user_text: Optional[str],
    extra_system: str = "",
    cancel: Optional[threading.Event] = None,
) -> Optional[str]:
    """Send the chat history plus the new message to Gemini, return the reply.

    Gemini 3 models think by default, and maxOutputTokens caps thinking AND
    the visible answer together. Left alone, the model spends the whole budget
    reasoning and hands back a candidate with no text at all. A short chat reply
    needs no deep reasoning, so we ask for the lowest thinking level and keep a
    budget large enough that it can never be starved.
    """
    contents: List[dict] = [
        {"role": h["role"], "parts": [{"text": h["text"]}]} for h in history[key]
    ]
    if user_text is not None:
        contents.append({"role": "user", "parts": [{"text": user_text}]})

    global GEMINI_MODEL

    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}

    def build(thinking: bool, max_tokens: int) -> dict:
        gen: Dict[str, Any] = {
            "temperature": TEMPERATURE,
            "maxOutputTokens": max_tokens,
        }
        if thinking and THINKING_LEVEL:
            # Nested - a flat "thinkingLevel" in generationConfig is a 400.
            gen["thinkingConfig"] = {"thinkingLevel": THINKING_LEVEL}
        return {
            "system_instruction": {
                "parts": [{"text": PERSONA + SYSTEM_SUFFIX + extra_system + now_line()}]
            },
            "contents": contents,
            "generationConfig": gen,
        }

    global PINNED_UNTIL

    # A model we fell back to is kept only for a while - quota and overload are
    # both temporary, and we want the good model back once they pass.
    if PINNED_UNTIL and time.time() > PINNED_UNTIL and GEMINI_MODEL != PRIMARY_MODEL:
        log.info("  -> fallback expired, back to %s", PRIMARY_MODEL)
        GEMINI_MODEL = PRIMARY_MODEL
        PINNED_UNTIL = 0.0

    use_thinking = bool(THINKING_LEVEL)
    max_tokens = MAX_OUTPUT_TOKENS
    models = [GEMINI_MODEL] + [m for m in (MODEL_CANDIDATES or []) if m != GEMINI_MODEL]
    model_idx = 0
    attempt = 0

    # With other providers behind us there is no point grinding through a long
    # backoff here - switching costs half a second, waiting costs the customer.
    have_backup = len(PROVIDERS) > 1
    max_attempts = 2 if have_backup else 6

    while attempt < max_attempts and model_idx < len(models):
        # Give up the moment the reply is called off. Without this a retry
        # storm (429s, an overloaded model) would hold the chat's lock for a
        # minute while the customer waits on a message we will never send.
        if cancel is not None and cancel.is_set():
            log.info("  -> abandoning the Gemini call, reply was called off")
            return None
        model = models[model_idx]
        url = f"{GEMINI_API}/models/{model}:generateContent"
        started = time.time()
        try:
            r = session.post(
                url, headers=headers, json=build(use_thinking, max_tokens),
                timeout=GEMINI_TIMEOUT,
            )
        except Exception as exc:
            attempt += 1
            log.warning("  -> gemini failed after %.0fs: %s", time.time() - started, exc)
            if cancel is not None:
                if cancel.wait(min(2 ** attempt, 15)):
                    return None
            else:
                time.sleep(min(2 ** attempt, 15))
            continue

        elapsed = time.time() - started

        if r.status_code == 200:
            if model != GEMINI_MODEL:
                log.warning("  -> %s worked, using it for the next %d min",
                            model, FALLBACK_MINUTES)
                GEMINI_MODEL = model
                PINNED_UNTIL = time.time() + FALLBACK_MINUTES * 60
            data = r.json()
            cands = data.get("candidates") or []
            if not cands:
                log.warning("  -> gemini: no candidates (%s)", data.get("promptFeedback"))
                return None
            cand = cands[0]
            parts = (cand.get("content") or {}).get("parts") or []
            # Thinking models emit internal "thought" parts - skip those.
            text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
            if text:
                log.info("  -> gemini 200 in %.1fs, %d chars", elapsed, len(text))
                return text

            reason = cand.get("finishReason")
            usage = data.get("usageMetadata") or {}
            log.warning(
                "  -> gemini 200 in %.1fs but no text (finishReason=%s, thoughts=%s tokens)",
                elapsed, reason, usage.get("thoughtsTokenCount"),
            )
            # Budget was eaten by reasoning: retry once, thinking off, bigger cap.
            if reason == "MAX_TOKENS" and (use_thinking or max_tokens < 4096):
                use_thinking = False
                max_tokens = max(max_tokens, 4096)
                log.info("  -> retrying with thinking off and %d tokens", max_tokens)
                continue
            return None

        # Some models reject thinkingConfig - drop it and try again. This costs
        # no attempt: the request was never really made with valid settings.
        if r.status_code == 400 and use_thinking and "think" in r.text.lower():
            log.warning("  -> %s rejected thinkingConfig, retrying without it", model)
            use_thinking = False
            continue

        attempt += 1

        # 429 = free-tier quota, 5xx = Google overloaded. Both are per-model,
        # so the fastest cure is a different model, not a longer wait.
        if r.status_code == 429 or r.status_code in (500, 502, 503, 504):
            log.warning("  -> gemini %s on %s: %s", r.status_code, model,
                        r.text[:140].replace("\n", " "))
            if model_idx + 1 < len(models):
                model_idx += 1
                log.info("  -> switching to %s", models[model_idx])
                continue  # straight to the next model, no waiting
            if have_backup:
                log.info("  -> handing over to the next provider instead of waiting")
                return None
            wait = min(4 * attempt, 20) + random.random()
            log.info("  -> all models busy, waiting %.0fs", wait)
            if cancel is not None:
                if cancel.wait(wait):
                    return None
            else:
                time.sleep(wait)
            continue

        log.error("  -> gemini %s: %s", r.status_code, r.text[:400])
        return None

    log.warning("  -> gave up after %d attempts across %d model(s)", attempt, model_idx + 1)
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
    if time.time() - last_reply_at.get(key, 0) < REPLY_COOLDOWN:
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


GROUP_NOTE = """

GROUP CHAT - THIS OVERRIDES THE ONE-LINE RULE
You are in a group with several people. Every incoming line is prefixed with \
the name of whoever said it. Never prefix your own replies with a name; use \
someone's name only when it matters who you are answering.

Here you may take room. Two or three sentences is normal, and a short riff is \
fine when the subject deserves one - this is a conversation, not a support \
desk. Still one paragraph, no line breaks, no lists, no headings, no emoji.

You are only ever writing here because somebody addressed you or was talking \
about you. Answer that - the person and the thing they actually said. Never \
open with an offer to help and never ask whether they need assistance.

TRADING INSULTS - THIS OVERRIDES THE ONE-TO-ONE RULE ABOVE
This is a room of people who know each other, not a customer chat. If someone \
comes at you, you give as good as you get: mock them back, call them what \
they are, and swear if they swore first. Aim it at the person who started it, \
land it in one line, and make it funny - the point is that they picked the \
wrong opponent, not that you are angry. Wit beats volume; a specific jab \
about what they just said beats any generic insult.

Where you stop, and this is absolute: nothing about anyone's ethnicity, \
nationality, religion, gender, sexuality, disability or illness; no threats \
of any kind; nothing sexual about them; nothing about their family; and no \
piling on a person somebody else is already dogpiling. Those are not \
comebacks, they are just ugly, and they would land on your account.

You never start it. You give one line back per jab, not three. If they stop, \
you stop - and if they are plainly upset rather than playing, you drop the \
whole thing at once.
"""


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


def judge_model() -> str:
    """A light model for the gate - it is a yes/no, not an essay."""
    if GROUP_JUDGE_MODEL:
        return GROUP_JUDGE_MODEL
    for m in MODEL_CANDIDATES:
        if "lite" in m:
            return m
    return GEMINI_MODEL


def judge_budget_ok(key: str) -> bool:
    now = time.time()
    calls = judge_calls[key]
    while calls and now - calls[0] > 60:
        calls.popleft()
    if len(calls) >= GROUP_JUDGE_MAX_PER_MIN:
        return False
    calls.append(now)
    return True


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

    for p in (x for x in PROVIDERS if not x.parked) or PROVIDERS:
        verdict = judge_via(p, question)
        if verdict is not None:
            reason = str(verdict.get("reason", ""))[:80]
            if verdict.get("speak"):
                return f"context: {reason}"
            log.info("  staying quiet - %s", reason)
            return ""
        log.info("  judge: %s unavailable, trying the next provider", p.name)
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


def judge_via(p: Provider, question: str) -> Optional[dict]:
    """Run the speak/stay-quiet decision on one provider. None = it failed."""
    if p.name != "gemini":
        # Strict JSON mode is the good path, but several free models cannot
        # honour it and answer 400. Falling back to plain text plus a tolerant
        # parser is far better than losing the judge entirely.
        modes = (False,) if p.no_json else (True, False)
        for strict in modes:
            raw = openai_chat(
                p, JUDGE_PROMPT + JUDGE_JSON_HINT,
                [{"role": "user", "content": question}],
                max_tokens=300, json_mode=strict, timeout=20,
            )
            if not raw:
                continue
            verdict = parse_json_loose(raw)
            if verdict is not None:
                return verdict
            log.warning("  judge: %s gave unparseable output", p.name)
        return None

    model = judge_model()
    url = f"{GEMINI_API}/models/{model}:generateContent"
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}

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
            return None
        if r.status_code == 400 and thinking and "think" in r.text.lower():
            continue
        if r.status_code != 200:
            log.warning("  judge %s: %s", r.status_code, r.text[:120].replace("\n", " "))
            return None
        try:
            parts = (r.json()["candidates"][0].get("content") or {}).get("parts") or []
            raw = "".join(x.get("text", "") for x in parts if not x.get("thought"))
            return json.loads(raw)
        except Exception as exc:
            log.warning("  judge gave unusable output: %s", exc)
            return None
    return None


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

    if not text or sender.get("is_bot") or sender.get("id") in IGNORE_USER_IDS:
        return
    if not group_allowed(chat_id):
        return

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
        return

    log.info("group %s | %s: %r  (%s)", chat_id, display_name(sender), clean[:60], reason)

    if time.time() - last_reply_at.get(key, 0) < REPLY_COOLDOWN:
        log.info("  -> ignored: within REPLY_COOLDOWN")
        return

    log.info("  -> answering...")
    started = time.time()
    answer = ask_ai(key, None, GROUP_NOTE)
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
    order, and REPLY_COOLDOWN stops it answering twice in a row.
    """
    chat = msg.get("chat") or {}
    key = f"group:{chat.get('id')}"

    def run() -> None:
        with chat_lock(key):
            try:
                handle_group_message(msg)
            except Exception:
                log.exception("error answering group %s", chat.get("id"))

    if ASYNC_REPLIES:
        EXECUTOR.submit(run)
    else:
        run()


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

    if ASYNC_REPLIES:
        EXECUTOR.submit(run)
    else:
        run()


def probe_provider(p: Provider) -> tuple:
    """One tiny real request. Returns (ok, detail, seconds)."""
    started = time.time()

    if p.name == "gemini":
        try:
            r = session.post(
                f"{GEMINI_API}/models/{GEMINI_MODEL}:generateContent",
                headers={"x-goog-api-key": p.key, "Content-Type": "application/json"},
                json={"contents": [{"role": "user", "parts": [{"text": "Reply with OK"}]}],
                      "generationConfig": {"maxOutputTokens": 1024, "temperature": 0}},
                timeout=25,
            )
        except Exception as exc:
            return False, f"network error: {exc}", time.time() - started
        took = time.time() - started
        return (True, GEMINI_MODEL, took) if r.status_code == 200 else (
            False, http_error(r), took)

    headers = {"Authorization": f"Bearer {p.key}", "Content-Type": "application/json"}
    if p.name == "openrouter":
        headers["HTTP-Referer"] = "https://t.me"
        headers["X-Title"] = "telegram-business-bot"
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
        "providers: " + ", ".join(
            f"{p.name}/{GEMINI_MODEL if p.name == 'gemini' else p.model}"
            for p in PROVIDERS
        ),
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


def handle_update(update: dict) -> None:
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
        EXECUTOR.submit(run_check) if ASYNC_REPLIES else run_check()
    elif text.startswith("/status"):
        tg("sendMessage", chat_id=msg["chat"]["id"], text=status_report())
    elif text.startswith("/start"):
        tg(
            "sendMessage",
            chat_id=msg["chat"]["id"],
            text=(
                "I'm alive. Connect me under Settings -> Telegram Business -> "
                "Chatbots and I'll answer your customers for you.\n\n"
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

ALLOWED_UPDATES = [
    "message",
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
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
        log.info("group trigger mode: %s%s", GROUP_TRIGGER,
                 f" (judge: {judge_model()})" if GROUP_TRIGGER == "context" else "")
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

    if any(p.name == "gemini" for p in PROVIDERS):
        pick_working_model()

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
        updates = tg("getUpdates", offset=offset, timeout=50, allowed_updates=ALLOWED_UPDATES)
        if updates is None:
            time.sleep(3)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            try:
                handle_update(update)
            except Exception:
                log.exception("error handling update %s", update.get("update_id"))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        save_history()
        log.info("bye")
