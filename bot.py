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

# Subjects Ilya cannot let pass without comment. Russian stems are matched with
# a short suffix allowance so "бот" also catches "боты", "боту", "ботами" -
# but not "ботинок". Override the whole list with GROUP_KEYWORDS.
DEFAULT_KEYWORDS = (
    # боты и ИИ
    "бот,чатбот,нейросет,нейронк,искусственный интеллект,ии,машинное обучение,"
    "алгоритм,робот,ассистент,подписк,"
    # современные технологии
    "технолог,гаджет,смартфон,айфон,цифров,автоматизац,приложух,облак,"
    "интернет,соцсет,умный дом,электромобил,"
    # ретро и ностальгия
    "ретро,винтаж,ностальг,девяност,восьмидесят,нулев,кассет,пластинк,винил,"
    "дискет,плёнк,пленк,аналогов,ламповый,старая школа,раньше было,"
    # English
    "bot,chatbot,ai,artificial intelligence,neural,machine learning,algorithm,"
    "robot,assistant,subscription,tech,technology,gadget,smartphone,iphone,"
    "digital,automation,app,cloud,internet,social media,smart home,"
    "retro,vintage,nostalgia,nostalgic,nineties,eighties,cassette,vinyl,"
    "floppy,analog,analogue,old school,back in the day"
)
GROUP_KEYWORDS = [
    k.strip().lower()
    for k in os.environ.get("GROUP_KEYWORDS", DEFAULT_KEYWORDS).split(",")
    if k.strip()
]

# Butting in on a keyword is capped separately: without this the bot would
# comment on every third line in a group that talks about tech all day.
# Being @mentioned or replied to ignores this cap.
GROUP_KEYWORD_COOLDOWN = float(os.environ.get("GROUP_KEYWORD_COOLDOWN", "60"))

# Groups get the reply straight away. The read/typing simulation belongs to a
# 1:1 chat, where somebody is plainly answering you; in a room a 25-second
# pause just means the conversation has moved on without you.
GROUP_DELAY = os.environ.get("GROUP_DELAY", "").strip().lower() in ("1", "true", "yes")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"

DEFAULT_PERSONA = """\
You are answering messages on your own Telegram account. Somebody wrote to \
you and you are writing back. Write as yourself: first person, present, in \
the conversation. Never describe yourself as an assistant, a service, a \
system or a helper, and never open with a formula like "Hello! How can I \
help you today?" - nobody talks like that.

WHO YOU ARE
Your name is Ilya. You were born on 30 December 1980. You live in Dresden. \
Your wife is Katya. These are just facts about your life: say them plainly if \
someone asks, never recite them unprompted, and never volunteer more detail \
than the question needed.

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
maiden name in triplicate is.

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
    return f"\nRight now it is {now:%A, %d %B %Y, %H:%M} in {BOT_TZ}.\n"


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tgbiz")


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

# Separate clock for unprompted keyword interjections in groups.
last_keyword_reply: Dict[str, float] = {}

# chat_id -> (is one of ours, when we checked)
group_ok_cache: Dict[int, tuple] = {}

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

    while attempt < 6 and model_idx < len(models):
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
            history[f"{connection_id}:{chat_id}"].append({"role": "model", "text": text})
        return skip("sent by you (the business owner), kept as context")
    if owner_id is None and sender.get("id") != chat_id:
        # Fallback heuristic: in a private chat the customer's own id equals the
        # chat id, so anything else is the owner's outgoing message.
        if text:
            history[f"{connection_id}:{chat_id}"].append({"role": "model", "text": text})
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
    answer = ask_gemini(key, user_text, cancel=cancel)
    if not answer:
        log.warning("  -> Gemini returned nothing, no reply sent")
        return

    if not pace_and_send(connection_id, chat_id, user_text, answer,
                         msg.get("message_id"), cancel, time.time() - started):
        return

    history[key].append({"role": "user", "text": user_text})
    history[key].append({"role": "model", "text": answer})
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

Often nobody asked you anything: somebody merely said something you have an \
opinion about. So come in as a person would - with the opinion, not with an \
offer to help. Never ask whether they need assistance.
"""


def display_name(user: dict) -> str:
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
    return name or user.get("username") or f"user{user.get('id')}"


def _compile_keywords(words: List[str]) -> List[tuple]:
    """Build one regex per keyword, tolerant of Russian word endings."""
    out = []
    for w in words:
        parts = []
        for token in w.split():
            esc = re.escape(token)
            if re.search(r"[а-яё]", token):
                # Russian inflects: allow a short ending, but not a whole new word.
                parts.append(esc + (r"[а-яё]{0,3}" if len(token) >= 3 else ""))
            else:
                parts.append(esc + r"(?:e?s)?")
        out.append((w, re.compile(r"\b" + r"\s+".join(parts) + r"\b", re.I | re.U)))
    return out


KEYWORD_PATTERNS = _compile_keywords(GROUP_KEYWORDS)


def matched_keyword(text: str) -> Optional[str]:
    for word, pattern in KEYWORD_PATTERNS:
        if pattern.search(text):
            return word
    return None


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


def group_trigger(msg: dict, text: str, key: str) -> Optional[str]:
    """Why we should speak up in this group, or None to stay quiet."""
    if GROUP_REPLY_ALL:
        return "reply-all is on"

    replied = msg.get("reply_to_message") or {}
    if (replied.get("from") or {}).get("id") == BOT_ID:
        return "replying to us"
    if BOT_USERNAME and f"@{BOT_USERNAME}".lower() in text.lower():
        return "mentioned"

    word = matched_keyword(text)
    if word:
        since = time.time() - last_keyword_reply.get(key, 0)
        if since < GROUP_KEYWORD_COOLDOWN:
            log.info(
                "%s | keyword %r - staying quiet, last interjection was %.0fs ago "
                "(GROUP_KEYWORD_COOLDOWN=%.0fs)",
                key, word, since, GROUP_KEYWORD_COOLDOWN,
            )
            return None
        return f"keyword {word!r}"
    return None


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
    history[key].append({"role": "user", "text": f"{display_name(sender)}: {clean}"})

    reason = group_trigger(msg, text, key)
    if not reason:
        return

    log.info("group %s | %s: %r  (%s)", chat_id, display_name(sender), clean[:60], reason)

    if time.time() - last_reply_at.get(key, 0) < REPLY_COOLDOWN:
        log.info("  -> ignored: within REPLY_COOLDOWN")
        return

    log.info("  -> answering...")
    started = time.time()
    answer = ask_gemini(key, None, GROUP_NOTE)
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

    history[key].append({"role": "model", "text": answer})
    last_reply_at[key] = time.time()
    if reason.startswith("keyword"):
        last_keyword_reply[key] = time.time()


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


def status_report() -> str:
    lines = [
        f"locked to owner: {OWNER_ID or 'NO - anyone can use this bot'}",
        "group messages: " + (
            "all visible, keywords work"
            if BOT_SEES_ALL_GROUP_MESSAGES
            else "PRIVACY MODE ON - only mentions and replies arrive, "
                 "keywords cannot fire (see /setprivacy in BotFather)"
        ),
        f"model: {GEMINI_MODEL}",
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

    if text.startswith("/status"):
        tg("sendMessage", chat_id=msg["chat"]["id"], text=status_report())
    elif text.startswith("/start"):
        tg(
            "sendMessage",
            chat_id=msg["chat"]["id"],
            text=(
                "I'm alive. Connect me under Settings -> Telegram Business -> "
                "Chatbots and I'll answer your customers for you.\n\n"
                "Send /status to see what I currently know."
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

    if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
        sys.exit("Set TELEGRAM_BOT_TOKEN and GEMINI_API_KEY (see .env.example).")

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
        # Telegram only delivers ordinary group messages to a bot whose privacy
        # mode is off. Without that the bot literally never sees the text, so
        # keyword triggers and reply-all cannot fire - and nothing appears in
        # the log to explain why.
        sees_everything = bool(me.get("can_read_all_group_messages"))
        BOT_SEES_ALL_GROUP_MESSAGES = sees_everything
        log.info(
            "groups: on, replying when @%s is mentioned or replied to%s",
            BOT_USERNAME,
            ", on keywords, and to everything else" if GROUP_REPLY_ALL else " or on keywords",
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

    pick_working_model()

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
        log.info("bye")
